from __future__ import annotations

import json
import time
from typing import Any

from curl_cffi import requests


TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
SECRET_KEYS = frozenset(
    {
        "access_token",
        "accessToken",
        "refresh_token",
        "refreshToken",
        "id_token",
        "idToken",
        "password",
        "session_token",
        "cookie",
        "cookies",
        "auth_key",
        "authorization",
    }
)
ACCOUNT_IMPORT_FIELDS = (
    "id",
    "email",
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "token_type",
    "expires_in",
    "cookies",
    "session_token",
    "user_agent",
    "source_type",
    "registration_channel",
    "created_at",
    "proxy",
    "oai-device-id",
    "oai-session-id",
)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key) in SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _safe_detail(value: Any, limit: int = 600) -> str:
    try:
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(_redact(value), ensure_ascii=False)
        else:
            text = str(value or "")
    except Exception:
        text = "云端接口返回了不可序列化的错误详情"
    return text[:limit]


def account_import_payload(account: dict[str, Any]) -> dict[str, Any]:
    """Keep cloud import focused on credentials and registration metadata.

    Local runtime fields such as health counters, survival timers, and cloud
    sync markers are intentionally excluded from the IMAGE POOL account
    record.
    """
    payload: dict[str, Any] = {}
    for field in ACCOUNT_IMPORT_FIELDS:
        value = account.get(field)
        if value is None or value == "":
            continue
        payload[field] = value
    if not str(payload.get("access_token") or "").strip():
        raise ValueError("云端上传账号缺少 access_token")
    return payload


def capacity_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    root = payload if isinstance(payload, dict) else {}
    estimate = root.get("estimate") if isinstance(root.get("estimate"), dict) else {}

    def value(name: str, default: Any = None) -> Any:
        return estimate.get(name, root.get(name, default))

    status = str(value("status") or "").strip().lower()
    if status not in {"idle", "enough", "saturated", "shortage"}:
        status = "unknown"
    result = {
        "status": status,
        "recommended_register_accounts": _bounded_int(
            value("recommended_register_accounts"), 0, 0, 100_000
        ),
        "recommended_add_usable_accounts": _bounded_int(
            value("recommended_add_usable_accounts"), 0, 0, 100_000
        ),
        "current_effective_accounts": _bounded_int(
            value("current_effective_accounts"), 0, 0, 100_000
        ),
        "current_effective_inflight_slots": _bounded_int(
            value("current_effective_inflight_slots"), 0, 0, 1_000_000
        ),
        "recommended_required_usable_accounts": _bounded_int(
            value("recommended_required_usable_accounts"), 0, 0, 100_000
        ),
        "recommended_required_inflight_slots": _bounded_int(
            value("recommended_required_inflight_slots"), 0, 0, 1_000_000
        ),
        "estimated_quota_capacity": max(0.0, float(value("estimated_quota_capacity") or 0)),
        "average_quota_per_usable_account": max(0.0, float(value("average_quota_per_usable_account") or 0)),
        "message": str(value("message") or "").strip(),
    }
    return result


class CloudRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)


class CloudClient:
    def __init__(self, settings: dict[str, Any], proxy: str = "") -> None:
        self.settings = settings
        self.server = str(settings.get("server") or "").strip().rstrip("/")
        self.auth_key = str(settings.get("auth_key") or "").strip()
        self.proxy = str(proxy or "").strip() if bool(settings.get("use_proxy", True)) else ""
        self.timeout = max(10.0, float(settings.get("timeout") or 30))
        self.retries = _bounded_int(settings.get("request_retries"), 2, 0, 5)
        try:
            self.retry_backoff = max(0.1, min(5.0, float(settings.get("retry_backoff_seconds") or 0.5)))
        except (TypeError, ValueError):
            self.retry_backoff = 0.5

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
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
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
                status_code = int(response.status_code or 0)
                try:
                    data = response.json()
                except Exception:
                    data = None

                if status_code < 200 or status_code >= 300:
                    detail = _safe_detail(data if data is not None else response.text)
                    error = CloudRequestError(
                        f"云端接口 HTTP {status_code}: {detail}",
                        status_code=status_code,
                    )
                    if status_code in TRANSIENT_HTTP_STATUSES and attempt < self.retries:
                        last_error = error
                        time.sleep(self.retry_backoff * (2**attempt))
                        continue
                    raise error
                if not isinstance(data, dict):
                    raise CloudRequestError(
                        f"云端接口返回格式错误: HTTP {status_code}: {_safe_detail(response.text, 500)}",
                        status_code=status_code,
                    )
                return data
            except CloudRequestError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise CloudRequestError(f"云端接口请求失败: {str(exc)[:500]}") from exc
                time.sleep(self.retry_backoff * (2**attempt))
            finally:
                session.close()
        raise CloudRequestError(f"云端接口请求失败: {str(last_error or 'unknown')[:500]}")

    def capacity(self) -> dict[str, Any]:
        limit = _bounded_int(self.settings.get("capacity_limit"), 60, 10, 200)
        return self._request("GET", "/api/image-pool/capacity", params={"limit": limit})

    def account_summary(self) -> dict[str, Any]:
        return self._request("GET", "/api/accounts/summary")

    def upload_account(self, account: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/accounts",
            payload={"tokens": [], "accounts": [account_import_payload(account)]},
            timeout=max(30.0, float(self.settings.get("upload_timeout") or 180)),
        )
