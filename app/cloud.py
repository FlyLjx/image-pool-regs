from __future__ import annotations

import json
from typing import Any

from curl_cffi import requests


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def capacity_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    estimate = payload.get("estimate") if isinstance(payload.get("estimate"), dict) else {}
    status = str(estimate.get("status") or "").strip().lower()
    if status not in {"idle", "enough", "saturated", "shortage"}:
        status = "unknown"
    return {
        "status": status,
        "recommended_register_accounts": _bounded_int(
            estimate.get("recommended_register_accounts"), 0, 0, 100_000
        ),
        "recommended_add_usable_accounts": _bounded_int(
            estimate.get("recommended_add_usable_accounts"), 0, 0, 100_000
        ),
        "current_effective_accounts": _bounded_int(
            estimate.get("current_effective_accounts"), 0, 0, 100_000
        ),
        "message": str(estimate.get("message") or "").strip(),
    }


class CloudClient:
    def __init__(self, settings: dict[str, Any], proxy: str = "") -> None:
        self.settings = settings
        self.server = str(settings.get("server") or "").strip().rstrip("/")
        self.auth_key = str(settings.get("auth_key") or "").strip()
        self.proxy = str(proxy or "").strip() if bool(settings.get("use_proxy", True)) else ""
        self.timeout = max(10.0, float(settings.get("timeout") or 30))

    def _validate(self) -> None:
        if not self.server:
            raise ValueError("请先配置云端地址")
        if not self.auth_key:
            raise ValueError("请先配置云端管理员密钥")

    def _session(self):
        options: dict[str, Any] = {"impersonate": "chrome", "verify": False}
        if self.proxy:
            options["proxy"] = self.proxy
        return requests.Session(**options)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._validate()
        safe_path = "/" + str(path or "").strip().lstrip("/")
        session = self._session()
        try:
            response = session.request(
                method.upper(),
                f"{self.server}{safe_path}",
                headers={
                    "Authorization": f"Bearer {self.auth_key}",
                    "Content-Type": "application/json",
                },
                params=params,
                json=payload,
                timeout=timeout or self.timeout,
                verify=False,
            )
            try:
                data = response.json()
            except Exception as exc:
                raise RuntimeError(
                    f"云端接口返回格式错误: HTTP {response.status_code}: {response.text[:500]}"
                ) from exc
            if response.status_code < 200 or response.status_code >= 300:
                detail = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
                raise RuntimeError(f"云端接口 HTTP {response.status_code}: {detail[:600]}")
            if not isinstance(data, dict):
                raise RuntimeError("云端接口响应必须是 JSON 对象")
            return data
        finally:
            session.close()

    def capacity(self) -> dict[str, Any]:
        limit = _bounded_int(self.settings.get("capacity_limit"), 60, 10, 200)
        return self._request("GET", "/api/image-pool/capacity", params={"limit": limit})

    def account_summary(self) -> dict[str, Any]:
        return self._request("GET", "/api/accounts/summary")

    def upload_account(self, account: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/accounts",
            payload={"tokens": [], "accounts": [account]},
            timeout=max(30.0, float(self.settings.get("upload_timeout") or 180)),
        )

