from __future__ import annotations

import copy
import json
import threading
from typing import Any, Callable

from curl_cffi import requests


DEFAULT_EMAIL001_API_BASE = "https://email001.com"
DEFAULT_EMAIL001_SKU_ID = 14
DEFAULT_EMAIL001_QUANTITY = 100
_DATA_KEYS = {
    "data", "items", "item", "list", "cards", "card", "secrets", "secret", "card_secret",
    "content", "delivery", "delivery_data", "download", "payload", "result", "results",
    "stock", "goods", "accounts", "account", "value",
}


class Email001PurchaseError(RuntimeError):
    pass


def _as_mailbox(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    email = str(value.get("email") or value.get("address") or value.get("username") or value.get("account") or "").strip()
    client_id = str(value.get("client_id") or value.get("clientId") or value.get("clientID") or "").strip()
    refresh_token = str(
        value.get("refresh_token")
        or value.get("refreshToken")
        or value.get("outlook_refresh_token")
        or value.get("token")
        or ""
    ).strip()
    if not email or "@" not in email or not client_id or not refresh_token:
        return None
    return {
        "email": email,
        "password": str(value.get("password") or value.get("passwd") or ""),
        "client_id": client_id,
        "refresh_token": refresh_token,
    }


def _text_items(value: str) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith(("[", "{")):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None:
            return _collect_items(decoded, hinted=True)
    result: list[dict[str, Any]] = []
    for line in text.splitlines():
        pieces = [piece.strip() for piece in line.strip().split("----")]
        if len(pieces) < 4 or "@" not in pieces[0]:
            continue
        result.append({
            "email": pieces[0],
            "password": pieces[1],
            "client_id": pieces[2],
            "refresh_token": "----".join(pieces[3:]),
        })
    return result


def _collect_items(value: Any, *, hinted: bool = False, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    direct = _as_mailbox(value)
    if direct is not None:
        return [direct]
    if isinstance(value, str):
        return _text_items(value) if hinted else []
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_collect_items(item, hinted=hinted, depth=depth + 1))
        return result
    if not isinstance(value, dict):
        return []
    result: list[dict[str, Any]] = []
    for key, item in value.items():
        key_name = str(key or "").strip().lower()
        if hinted or key_name in _DATA_KEYS:
            result.extend(_collect_items(item, hinted=True, depth=depth + 1))
    return result


def extract_mailboxes(payload: Any) -> list[dict[str, Any]]:
    """Extract import-compatible Outlook rows from an order response.

    email001 delivery responses have appeared as a text block, a list of
    objects, and nested ``data/items/cards`` objects. Keep the parser tolerant
    while requiring the four fields used by the existing Outlook pool.
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _collect_items(payload, hinted=True):
        email = str(item.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        result.append(copy.deepcopy(item))
    return result


class Email001AutoPurchaseClient:
    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, settings: dict[str, Any] | None = None, *, proxy: str = "", request_timeout: float = 30) -> None:
        values = settings if isinstance(settings, dict) else {}
        self.enabled = bool(values.get("email001_auto_purchase", False))
        self.api_key = str(values.get("email001_api_key") or "").strip()
        self.api_base = str(values.get("email001_api_base") or DEFAULT_EMAIL001_API_BASE).strip().rstrip("/")
        self.sku_id = max(1, int(values.get("email001_sku_id") or DEFAULT_EMAIL001_SKU_ID))
        self.quantity = max(1, min(1000, int(values.get("email001_quantity") or DEFAULT_EMAIL001_QUANTITY)))
        self.request_timeout = max(10.0, float(values.get("email001_purchase_timeout") or request_timeout or 30))
        options: dict[str, Any] = {"impersonate": "chrome", "verify": False}
        if proxy:
            options["proxy"] = proxy
        self.session = requests.Session(**options)

    def close(self) -> None:
        self.session.close()

    def _lock(self) -> threading.Lock:
        key = f"{self.api_base}|{self.api_key}"
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def purchase_and_import(
        self,
        *,
        available_slots: Callable[[], int],
        import_payload: Callable[[Any], dict[str, Any]],
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if available_slots() > 0:
            return {"purchased": False, "available_slots": available_slots(), "reason": "stock_available"}
        if not self.enabled:
            return {"purchased": False, "available_slots": 0, "reason": "disabled"}
        if not self.api_key:
            raise Email001PurchaseError("Outlook 号池已枯竭，email001 自动购买已开启但缺少 API Key")
        with self._lock():
            current = available_slots()
            if current > 0:
                return {"purchased": False, "available_slots": current, "reason": "stock_refilled_by_other_worker"}
            if on_status:
                on_status(f"Outlook 号池已枯竭，正在从 email001 下单 {self.quantity} 个邮箱")
            response = self.session.request(
                "POST",
                f"{self.api_base}/api/v1/open/orders",
                json={"api_key": self.api_key, "sku_id": self.sku_id, "quantity": self.quantity},
                headers={"accept": "application/json", "content-type": "application/json"},
                timeout=self.request_timeout,
                verify=False,
            )
            try:
                payload = response.json() if response.text else {}
            except Exception as exc:
                raise Email001PurchaseError(f"email001 下单响应不是有效 JSON：{exc}") from exc
            if response.status_code >= 400 or not isinstance(payload, dict) or int(payload.get("status_code") or 0) not in {0, 200}:
                detail = str(payload.get("msg") or payload.get("message") or response.text[:300]).strip()
                raise Email001PurchaseError(f"email001 下单失败：HTTP {response.status_code}：{detail}")
            items = extract_mailboxes(payload.get("data", payload))
            if not items:
                raise Email001PurchaseError("email001 下单成功但返回数据中没有可导入的 Outlook 邮箱")
            imported = import_payload(items)
            remaining = available_slots()
            if remaining <= 0:
                raise Email001PurchaseError("email001 返回邮箱已导入，但号池仍没有可用分裂槽位")
            if on_status:
                on_status(f"email001 下单完成：返回 {len(items)} 个，新增 {int(imported.get('added') or 0)} 个，当前可用 {remaining} 个")
            return {
                "purchased": True,
                "requested": self.quantity,
                "returned": len(items),
                "imported": imported,
                "available_slots": remaining,
            }
