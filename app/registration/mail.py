from __future__ import annotations

import html
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable

from curl_cffi import requests


AUTO_DOMAIN_MARKERS = {"", "auto", "自动", "自动选择"}
TRANSIENT_STATUSES = {408, 425, 429, 500, 501, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526}


def _suffix() -> str:
    timestamp = format(int(time.time() * 1000), "x")[-8:]
    return f"{timestamp}{secrets.token_hex(2)}"


def _local_part(prefix: str = "") -> str:
    if prefix.strip():
        return f"{prefix.strip()}_{_suffix()}"
    head = "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))
    return f"{head}{_suffix()}"


def extract_otp(message: dict[str, Any]) -> str | None:
    content = "\n".join(
        str(message.get(key) or "")
        for key in ("subject", "text", "text_content", "html", "html_content", "body", "content")
    )
    content = html.unescape(re.sub(r"<[^>]+>", " ", content))
    patterns = (
        r"(?:verification code|code is|one-time code|验证码|代码为)[:：\s-]*(\d{6})",
        r"(?<![#&\d])(\d{6})(?!\d)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            code = match.group(1)
            if code != "177010":
                return code
    return None


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


class YydsMailClient:
    provider_name = "yyds"
    _domain_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
    _domain_cache_lock = Lock()
    _domain_cache_ttl = 300.0

    def __init__(self, settings: dict[str, Any], proxy: str = "") -> None:
        self.api_base = str(settings.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(settings.get("api_key") or "").strip()
        if not self.api_key:
            raise ValueError("请先在设置中填写 YYDS Mail API Key")
        self.domains = [str(item).strip() for item in settings.get("domains", []) if str(item).strip()]
        self.email_prefix = str(settings.get("email_prefix") or "").strip()
        self.request_timeout = max(10.0, float(settings.get("request_timeout") or 30))
        options: dict[str, Any] = {"impersonate": "chrome", "verify": False}
        if proxy:
            options["proxy"] = proxy
        self.session = requests.Session(**options)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        response = None
        last_error = ""
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    f"{self.api_base}{path}",
                    headers=headers,
                    params=params,
                    json=payload,
                    timeout=self.request_timeout,
                    verify=False,
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt == 3:
                    raise RuntimeError(f"邮箱接口请求异常: {last_error[:240]}") from exc
                time.sleep(min(8.0, 2**attempt))
                continue
            if response.status_code not in TRANSIENT_STATUSES or attempt == 3:
                break
            time.sleep(min(8.0, 2**attempt))
        if response is None:
            raise RuntimeError(f"邮箱接口没有响应: {last_error}")
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(f"邮箱接口 HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code == 204:
            return {}
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"邮箱接口返回失败: {data.get('errorCode') or data.get('error') or 'unknown'}")
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            return data["data"]
        return data

    @staticmethod
    def _domain_items(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "domains", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def list_domains(self, *, refresh: bool = False) -> list[str]:
        cache_key = (self.api_base, self.api_key)
        now = time.monotonic()
        if not refresh:
            with self._domain_cache_lock:
                cached = self._domain_cache.get(cache_key)
                if cached and now - cached[0] < self._domain_cache_ttl:
                    return list(cached[1])

        data = self._request("GET", "/domains")
        domains: list[str] = []
        seen: set[str] = set()
        for item in self._domain_items(data):
            if isinstance(item, str):
                domain = item.strip().lower()
            elif isinstance(item, dict):
                if item.get("isPublic") is False or item.get("isVerified") is False:
                    continue
                if item.get("isMxValid") is False:
                    continue
                dns = item.get("dnsRecords") if isinstance(item.get("dnsRecords"), dict) else {}
                if dns.get("receivingReady") is False:
                    continue
                domain = str(item.get("domain") or item.get("name") or "").strip().lower()
            else:
                continue
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
        if not domains:
            raise RuntimeError("邮箱接口没有返回可收信域名")
        with self._domain_cache_lock:
            self._domain_cache[cache_key] = (now, list(domains))
        return domains

    def create_mailbox(self, domain: str | None = None, local_part: str | None = None) -> dict[str, str]:
        payload: dict[str, str] = {"localPart": str(local_part).strip() if local_part else _local_part(self.email_prefix)}
        selected_domain = str(domain).strip() if domain is not None else (
            random.choice(self.domains) if self.domains else "auto"
        )
        if selected_domain.lower() not in AUTO_DOMAIN_MARKERS:
            payload["domain"] = selected_domain
        data = self._request("POST", "/accounts", payload=payload)
        if not isinstance(data, dict):
            raise RuntimeError("邮箱创建响应格式不正确")
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(
            data.get("token")
            or data.get("temp_token")
            or data.get("tempToken")
            or data.get("access_token")
            or ""
        ).strip()
        if not address or not token:
            raise RuntimeError("邮箱创建响应缺少 address 或 token")
        return {"address": address, "token": token, "account_id": str(data.get("id") or "")}

    @staticmethod
    def existing_mailbox(email: str) -> dict[str, str]:
        address = str(email or "").strip()
        if not address:
            raise ValueError("登录恢复缺少邮箱地址")
        return {"address": address, "token": "", "account_id": ""}

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            values = data.get("items") or data.get("messages") or data.get("data") or []
            return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
        return []

    def wait_for_code(
        self,
        mailbox: dict[str, str],
        *,
        requested_at: float,
        timeout: float,
        interval: float,
        stopped: Callable[[], bool] | None = None,
        excluded_codes: set[str] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str | None:
        deadline = time.monotonic() + max(10.0, timeout)
        seen: set[str] = set()
        excluded = {str(code).strip() for code in (excluded_codes or set()) if str(code).strip()}
        poll_count = 0
        last_status_at = 0.0
        while time.monotonic() < deadline:
            if stopped and stopped():
                raise RuntimeError("任务已停止")
            data = self._request(
                "GET",
                "/messages",
                token=mailbox["token"],
                params={"address": mailbox["address"]},
            )
            poll_count += 1
            messages = sorted(
                self._items(data),
                key=lambda item: _timestamp(
                    item.get("createdAt")
                    or item.get("created_at")
                    or item.get("receivedAt")
                    or item.get("date")
                    or item.get("timestamp")
                ),
                reverse=True,
            )
            for item in messages:
                message_id = str(item.get("id") or item.get("message_id") or "").strip()
                identity = message_id or repr(sorted(item.items()))
                if identity in seen:
                    continue
                seen.add(identity)
                received_at = _timestamp(
                    item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")
                )
                if received_at and received_at < requested_at - 3:
                    continue
                detail = item
                if message_id:
                    loaded = self._request(
                        "GET",
                        f"/messages/{message_id}",
                        token=mailbox["token"],
                        params={"address": mailbox["address"]},
                    )
                    if isinstance(loaded, dict):
                        detail = loaded
                code = extract_otp(detail)
                if code and code not in excluded:
                    return code
            now = time.monotonic()
            if on_status and (poll_count == 1 or now - last_status_at >= 15.0):
                last_status_at = now
                on_status(f"邮箱轮询第 {poll_count} 次：读取到 {len(messages)} 封邮件，尚未发现新的验证码")
            time.sleep(max(0.5, interval))
        return None

    def existing_codes(self, mailbox: dict[str, str], limit: int = 50) -> set[str]:
        data = self._request(
            "GET",
            "/messages",
            token=mailbox["token"],
            params={"address": mailbox["address"]},
        )
        codes: set[str] = set()
        for item in self._items(data)[: max(1, int(limit))]:
            message_id = str(item.get("id") or item.get("message_id") or "").strip()
            detail = item
            if message_id:
                loaded = self._request(
                    "GET",
                    f"/messages/{message_id}",
                    token=mailbox["token"],
                    params={"address": mailbox["address"]},
                )
                if isinstance(loaded, dict):
                    detail = loaded
            code = extract_otp(detail)
            if code:
                codes.add(code)
        return codes
