from __future__ import annotations

import base64
import copy
import json
import random
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests

from app.registration.mail import YydsMailClient
from app.registration.outlook import OutlookMailClient
from app.registration.sentinel import build_so_token, build_standard_token


AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
AUDIENCE = "https://api.openai.com/v1"
AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
DEFAULT_OUTLOOK_POOL_PATH = Path(__file__).resolve().parents[2] / "data" / "outlook_mailboxes.json"


class WrongOtpError(RuntimeError):
    pass
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


def flaresolverr_proxy_url(proxy: str) -> str:
    value = str(proxy or "").strip()
    parsed = urlparse(value)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return value
    auth = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{auth}host.docker.internal{port}").geturl()

LogCallback = Callable[[str, str], None]


class AccountBannedError(RuntimeError):
    pass


class RegistrationDisallowedError(RuntimeError):
    pass


class CloudflareChallengeError(RuntimeError):
    pass


class ExistingAccountRouteError(RuntimeError):
    pass


def outlook_error_requires_disable(error: BaseException | str) -> bool:
    text = str(error or "").lower()
    return "registration_disallowed" in text or "拒绝创建账号资料" in text


def outlook_error_is_transient(error: BaseException | str) -> bool:
    text = str(error or "").lower()
    return any(
        marker in text
        for marker in (
            "任务已停止",
            "cloudflare",
            "flaresolverr",
            "challenge",
            "tls connect error",
            "curl: (35)",
            "openssl_internal",
            "err_proxy_connection_failed",
            "err_timed_out",
            "timeout after",
            "母号正在注册",
            "队列等待",
        )
    )


def outlook_error_should_disable(error: BaseException | str) -> bool:
    if outlook_error_requires_disable(error):
        return True
    if outlook_error_is_transient(error):
        return False
    return True


_TLS_TRANSPORT_MARKERS = (
    "curl: (35)",
    "tls connect error",
    "openssl_internal",
    "invalid library",
)


def generate_pkce() -> tuple[str, str]:
    import hashlib

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%"),
    ]
    required.extend(secrets.choice(alphabet) for _ in range(max(0, length - len(required))))
    random.SystemRandom().shuffle(required)
    return "".join(required)


def _trace_headers() -> dict[str, str]:
    parent_id = random.getrandbits(64)
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{parent_id:016x}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": str(parent_id),
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(random.getrandbits(64)),
    }


def _json(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _callback_code(value: str) -> str:
    if not value:
        return ""
    try:
        return str((parse_qs(urlparse(value).query).get("code") or [""])[0]).strip()
    except Exception:
        return ""


def _response_code(response: Any) -> str:
    data = _json(response)
    for field in ("continue_url", "redirect_url", "redirectUrl", "url"):
        code = _callback_code(str(data.get(field) or ""))
        if code:
            return code
    return _callback_code(str(getattr(response, "url", "") or "")) or str(
        data.get("authorization_code") or data.get("authorizationCode") or ""
    ).strip()


def _continue_url(response: Any) -> str:
    data = _json(response)
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    raw = str(
        payload.get("url")
        or data.get("continue_url")
        or data.get("redirect_url")
        or data.get("redirectUrl")
        or data.get("url")
        or getattr(response, "url", "")
        or ""
    ).strip()
    return urljoin(AUTH_BASE, raw) if raw else ""


def _authorization_route(value: str) -> str:
    path = urlparse(str(value or "")).path.rstrip("/").lower() or "/"
    if path == "/create-account/password":
        return "signup"
    if path == "/log-in" or path.startswith("/log-in/"):
        return "existing"
    if path == "/error":
        return "error"
    return "unknown"


def _authorization_error(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.path.rstrip("/").lower() != "/error":
        return ""
    encoded = str((parse_qs(parsed.query).get("payload") or [""])[0]).strip()
    if not encoded:
        return "authorization_error"
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return "authorization_error"
        return str(payload.get("errorCode") or payload.get("error_code") or payload.get("kind") or "authorization_error")
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return "authorization_error"


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _cloudflare_challenge(response: Any) -> bool:
    if response is None:
        return False
    text = str(getattr(response, "text", "") or "").lower()
    status = int(getattr(response, "status_code", 0) or 0)
    server = str((getattr(response, "headers", {}) or {}).get("server") or "").lower()
    content_marker = "challenges.cloudflare.com" in text or "<title>just a moment" in text
    return content_marker or ("cloudflare" in server and status in {403, 429, 503})


class ProtocolRegistrar:
    _flare_lock = Lock()
    _flare_cache: dict[str, dict[str, Any]] = {}
    _flare_failures: dict[str, tuple[float, str]] = {}
    _flare_cache_seconds = 900.0
    _flare_failure_seconds = 20.0
    _otp_send_lock = Lock()
    _otp_last_sent_at = 0.0
    _otp_send_interval = 1.5

    def __init__(
        self,
        settings: dict[str, Any],
        logger: LogCallback | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self.settings = settings
        self.registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        self.mail_settings = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        self.outlook_pool_path = Path(str(settings.get("outlook_pool_path") or DEFAULT_OUTLOOK_POOL_PATH))
        self.sentinel_settings = settings.get("sentinel") if isinstance(settings.get("sentinel"), dict) else {}
        self.flaresolverr = settings.get("flaresolverr") if isinstance(settings.get("flaresolverr"), dict) else {}
        self.proxy = str(self.registration.get("proxy") or "").strip()
        self.profile = str(self.registration.get("browser_profile") or "chrome_windows").strip().lower()
        self.user_agent = SAFARI_UA if self.profile == "safari_macos" else CHROME_UA
        self.impersonate = "safari180" if self.profile == "safari_macos" else "chrome145"
        self.timeout = max(15.0, float(self.registration.get("request_timeout") or 45))
        self.logger = logger or (lambda _level, _message: None)
        self.stop_event = stop_event or Event()
        options: dict[str, Any] = {"impersonate": self.impersonate, "verify": False}
        if self.proxy:
            options["proxy"] = self.proxy
        self.session = requests.Session(**options)
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.authorize_url = ""
        self._flare_cached_at = 0.0

    def close(self) -> None:
        self.session.close()

    def _session_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"impersonate": self.impersonate, "verify": False}
        if self.proxy:
            options["proxy"] = self.proxy
        return options

    @staticmethod
    def _is_tls_transport_error(error: BaseException | str) -> bool:
        detail = str(error or "").lower()
        return any(marker in detail for marker in _TLS_TRANSPORT_MARKERS)

    def _rebuild_transport_session(self) -> None:
        previous_session = self.session
        cookies: list[tuple[str, str, str, str]] = []
        try:
            for cookie in previous_session.cookies.jar:
                cookies.append(
                    (
                        str(cookie.name),
                        str(cookie.value),
                        str(cookie.domain or ""),
                        str(cookie.path or "/"),
                    )
                )
        except Exception:
            pass
        self.session = requests.Session(**self._session_options())
        for name, value, domain, path in cookies:
            try:
                self.session.cookies.set(name, value, domain=domain or None, path=path)
            except Exception:
                continue
        try:
            previous_session.close()
        except Exception:
            pass

    def _renew_authorization_session(self) -> None:
        previous_session = self.session
        self.session = requests.Session(**self._session_options())
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.authorize_url = ""
        try:
            previous_session.close()
        except Exception:
            pass

    def _log(self, message: str, level: str = "info") -> None:
        self.logger(level, message)

    def _mail_client(self, provider: str = "") -> Any:
        source = str(provider or self.mail_settings.get("provider") or "yyds").strip().lower()
        if source == "outlook":
            return OutlookMailClient(
                self.outlook_pool_path,
                proxy=self.proxy,
                request_timeout=self.timeout,
                split_limit=int(self.mail_settings.get("outlook_split_limit") or 5),
                queue_wait_timeout=max(
                    1800.0,
                    float(self.registration.get("mail_wait_timeout") or 120) * 8,
                    self.timeout * 20,
                ),
                stopped=self.stop_event.is_set,
                on_status=self._log,
            )
        mail_options = dict(self.mail_settings)
        mail_options["request_timeout"] = self.timeout
        return YydsMailClient(mail_options, self.proxy)

    def _check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise RuntimeError("任务已停止")

    def _client_hints(self) -> dict[str, str]:
        if self.profile == "safari_macos":
            return {}
        version_match = re.search(r"(?:Chrome|Chromium)/([\d.]+)", self.user_agent)
        full_version = version_match.group(1) if version_match else "145.0.0.0"
        major_version = full_version.split(".", 1)[0]
        platform = "Linux" if "Linux" in self.user_agent else "Windows"
        return {
            "sec-ch-ua": (
                f'"Google Chrome";v="{major_version}", "Not?A_Brand";v="8", '
                f'"Chromium";v="{major_version}"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{platform}"',
        }

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "max-age=0",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": self.user_agent,
            **self._client_hints(),
        }
        if referer:
            headers["referer"] = referer
        return headers

    def _api_headers(self, referer: str) -> dict[str, str]:
        return {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": AUTH_BASE,
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.user_agent,
            "oai-device-id": self.device_id,
            **self._client_hints(),
            **_trace_headers(),
        }


    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._check_stopped()
        last_error = ""
        for attempt in range(4):
            try:
                return self.session.request(method, url, timeout=self.timeout, **kwargs)
            except Exception as exc:
                last_error = str(exc)
                if self._is_tls_transport_error(exc):
                    self._log(
                        f"TLS 握手异常，已重建 HTTP 会话并重试 {attempt + 1}/4",
                        "warning",
                    )
                    self._rebuild_transport_session()
                if attempt < 3:
                    time.sleep(0.6 + attempt * 0.4)
        raise RuntimeError(f"请求失败: {last_error[:300]}")

    def _set_security_cookie(self, value: str) -> None:
        if not value:
            return
        for domain in (".openai.com", "openai.com", ".auth.openai.com", "auth.openai.com"):
            self.session.cookies.set("oai-sc", value, domain=domain, path="/")

    def _add_sentinel_headers(self, headers: dict[str, str], flow: str) -> None:
        standard, cookie = build_standard_token(
            self.session,
            self.device_id,
            flow,
            self.user_agent,
            self.sentinel_settings,
        )
        headers["openai-sentinel-token"] = standard
        self._set_security_cookie(cookie)
        if not bool(self.sentinel_settings.get("so_enabled", True)):
            return
        try:
            so_token, so_cookie = build_so_token(
                self.session,
                self.device_id,
                flow,
                self.user_agent,
                self.sentinel_settings,
            )
            self._set_security_cookie(so_cookie)
            if so_token:
                headers["OpenAI-Sentinel-SO-Token"] = so_token
        except Exception as exc:
            if bool(self.sentinel_settings.get("so_required", False)):
                raise
            self._log(f"SO Token 生成跳过: {str(exc)[:180]}", "warning")

    def _flaresolverr_endpoints(self) -> list[str]:
        api_url = str(self.flaresolverr.get("url") or "").strip().rstrip("/")
        if not api_url:
            return []
        endpoints = [api_url if api_url.endswith("/v1") else f"{api_url}/v1"]
        parsed = urlparse(api_url)
        if parsed.hostname == "flaresolverr":
            port = parsed.port or 8191
            endpoints.append(f"http://127.0.0.1:{port}/v1")
        return list(dict.fromkeys(endpoints))

    def _flare_cache_key(self, target_url: str = "") -> str:
        proxy = flaresolverr_proxy_url(self.proxy) if bool(self.flaresolverr.get("pass_proxy", True)) else "direct"
        host = urlparse(str(target_url or "")).hostname or "default"
        return f"{'|'.join(self._flaresolverr_endpoints())}|{proxy}|{self.profile}|{host.lower()}"

    def _cached_flare_solution(self, target_url: str = "") -> dict[str, Any] | None:
        cached = type(self)._flare_cache.get(self._flare_cache_key(target_url))
        if not cached or time.monotonic() - float(cached.get("cached_at") or 0) >= self._flare_cache_seconds:
            return None
        return cached

    def _apply_flare_solution(self, solution: dict[str, Any]) -> int:
        applied = 0
        for item in solution.get("cookies", []):
            if not isinstance(item, dict) or not self._shareable_flare_cookie(item):
                continue
            self.session.cookies.set(
                str(item["name"]),
                str(item.get("value") or ""),
                domain=str(item.get("domain") or ".openai.com"),
                path=str(item.get("path") or "/"),
            )
            applied += 1
        solved_ua = str(solution.get("userAgent") or "").strip()
        if solved_ua:
            self.user_agent = solved_ua
        self._flare_cached_at = float(solution.get("cached_at") or 0)
        return applied

    @staticmethod
    def _shareable_flare_cookie(item: dict[str, Any]) -> bool:
        name = str(item.get("name") or "").strip().lower()
        return (
            name in {"cf_clearance", "oai-did"}
            or name.startswith("__cf")
            or name.startswith("_cf")
            or name.startswith("cf_")
        )

    def _apply_cached_flare_solution(self, target_url: str = "") -> bool:
        if not bool(self.flaresolverr.get("enabled")):
            return False
        with type(self)._flare_lock:
            cached = self._cached_flare_solution(target_url)
            if not cached:
                return False
            self._apply_flare_solution(cached)
            return True

    def _solve_cloudflare(self, target_url: str, reason: str = "环境异常", force_refresh: bool = False) -> bool:
        enabled = bool(self.flaresolverr.get("enabled"))
        endpoints = self._flaresolverr_endpoints()
        if not enabled or not endpoints:
            return False
        while not type(self)._flare_lock.acquire(timeout=0.5):
            self._check_stopped()
        try:
            cache_key = self._flare_cache_key(target_url)
            cached = self._cached_flare_solution(target_url)
            if cached and (not force_refresh or float(cached.get("cached_at") or 0) > self._flare_cached_at):
                applied = self._apply_flare_solution(cached)
                self._log(f"复用 FlareSolverr 验证结果，导入 {applied} 个 Cookie")
                return True
            failure = type(self)._flare_failures.get(cache_key)
            if failure and time.monotonic() - failure[0] < self._flare_failure_seconds:
                raise RuntimeError(f"FlareSolverr 最近一次解题失败: {failure[1]}")

            payload: dict[str, Any] = {
                "cmd": "request.get",
                "url": target_url,
                "maxTimeout": max(1000, int(self.flaresolverr.get("max_timeout_ms") or 60000)),
            }
            if self.proxy and bool(self.flaresolverr.get("pass_proxy", True)):
                payload["proxy"] = {"url": flaresolverr_proxy_url(self.proxy)}
            self._log(f"检测到{reason}，启动 FlareSolverr 兜底", "warning")
            response = None
            data: dict[str, Any] = {}
            errors: list[str] = []
            for endpoint in endpoints:
                try:
                    response = requests.post(
                        endpoint,
                        json=payload,
                        timeout=max(15, payload["maxTimeout"] / 1000 + 10),
                        verify=False,
                    )
                except Exception as exc:
                    errors.append(f"{endpoint}: {str(exc)[:180]}")
                    continue
                data = _json(response)
                if response.status_code == 200 and str(data.get("status") or "").lower() == "ok":
                    break
                errors.append(f"{endpoint}: HTTP {response.status_code}: {response.text[:180]}")
                response = None
            if response is None:
                detail = "; ".join(errors)[-600:]
                type(self)._flare_failures[cache_key] = (time.monotonic(), detail)
                raise RuntimeError(f"FlareSolverr 兜底失败: {detail}")
            solution = data.get("solution") if isinstance(data.get("solution"), dict) else {}
            shared_cookies = [
                copy.deepcopy(item)
                for item in solution.get("cookies", [])
                if isinstance(item, dict) and self._shareable_flare_cookie(item)
            ]
            if not shared_cookies:
                detail = "解题响应未返回 Cloudflare Cookie"
                type(self)._flare_failures[cache_key] = (time.monotonic(), detail)
                raise RuntimeError(f"FlareSolverr 兜底失败: {detail}")
            cached_solution = copy.deepcopy(solution)
            cached_solution["cookies"] = shared_cookies
            cached_solution["cached_at"] = time.monotonic()
            type(self)._flare_cache[cache_key] = cached_solution
            type(self)._flare_failures.pop(cache_key, None)
            applied = self._apply_flare_solution(cached_solution)
            self._log(f"FlareSolverr 已导入 {applied} 个 Cookie")
            return True
        finally:
            type(self)._flare_lock.release()

    def _authorize(self, email: str, screen_hint: str = "login_or_signup") -> str:
        self._log(f"初始化 OAuth/PKCE 授权会话 ({screen_hint})")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com", path="/")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com", path="/")
        self._apply_cached_flare_solution(AUTH_BASE)
        self.code_verifier, code_challenge = generate_pkce()
        params = {
            "issuer": AUTH_BASE,
            "client_id": CLIENT_ID,
            "audience": AUDIENCE,
            "redirect_uri": REDIRECT_URI,
            "device_id": self.device_id,
            "screen_hint": screen_hint,
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": AUTH0_CLIENT,
        }
        self.authorize_url = f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}"
        try:
            response = self._request(
                "GET",
                self.authorize_url,
                headers=self._navigate_headers(f"{PLATFORM_BASE}/"),
                allow_redirects=True,
                verify=False,
            )
        except RuntimeError:
            if not self._solve_cloudflare(self.authorize_url, reason="直连网络异常"):
                raise
            response = self._request(
                "GET",
                self.authorize_url,
                headers=self._navigate_headers(f"{PLATFORM_BASE}/"),
                allow_redirects=True,
                verify=False,
            )
        if _cloudflare_challenge(response) and self._solve_cloudflare(
            str(response.url or self.authorize_url),
            reason="Cloudflare 验证页",
            force_refresh=True,
        ):
            response = self._request(
                "GET",
                self.authorize_url,
                headers=self._navigate_headers(f"{PLATFORM_BASE}/"),
                allow_redirects=True,
                verify=False,
            )
        if _cloudflare_challenge(response):
            self._log("OAuth 仍处于 Cloudflare 验证页，结束当前协议任务", "warning")
            raise CloudflareChallengeError("OAuth Cloudflare 验证未通过，当前出口仍被挑战页拦截")
        if response.status_code != 200:
            raise RuntimeError(f"OAuth authorize 失败: HTTP {response.status_code}: {response.text[:240]}")
        final_url = str(response.url or self.authorize_url)
        route = _authorization_route(final_url)
        labels = {
            "signup": "注册密码页",
            "existing": "已有账号登录页",
            "error": "授权错误页",
            "unknown": "未知页面",
        }
        self._log(f"OAuth 授权落点: {labels[route]} ({urlparse(final_url).path or '/'})")
        return final_url

    @staticmethod
    def _require_signup_route(final_url: str, email: str) -> None:
        route = _authorization_route(final_url)
        if route == "signup":
            return
        if route == "existing":
            raise ExistingAccountRouteError(f"邮箱已存在，OAuth 已进入登录步骤: {email}")
        if route == "error":
            code = _authorization_error(final_url)
            if code == "rate_limit_exceeded":
                raise RuntimeError("OAuth authorize 触发 rate_limit_exceeded，请稍后重试当前邮箱")
            raise RuntimeError(f"OAuth authorize 进入错误页: {code}")
        raise RuntimeError(f"OAuth authorize 未进入注册密码步骤，当前落点: {urlparse(str(final_url or '')).path or 'unknown'}")

    def _register_password(
        self,
        email: str,
        password: str,
        retry_auth_step: bool = True,
        retry_account_creation: bool = True,
    ) -> None:
        self._log("提交邮箱和随机密码")
        headers = self._api_headers(f"{AUTH_BASE}/create-account/password")
        self._add_sentinel_headers(headers, "username_password_create")
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers,
            verify=False,
        )
        if response.status_code != 200:
            data = _json(response)
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            error_code = str(error.get("code") or data.get("code") or "").strip().lower()
            if error_code == "invalid_auth_step" and retry_auth_step:
                self._log("授权步骤已失效，正在重建 HTTP 会话、Device ID 和 OAuth/PKCE", "warning")
                self._renew_authorization_session()
                final_url = self._authorize(email)
                self._require_signup_route(final_url, email)
                self._register_password(
                    email,
                    password,
                    retry_auth_step=False,
                    retry_account_creation=retry_account_creation,
                )
                return
            if error_code == "account_creation_failed" and retry_account_creation:
                self._log("账号创建事务被临时拒绝，正在重建 OAuth/Sentinel 会话后重试", "warning")
                if self.stop_event.wait(1.5):
                    raise RuntimeError("任务已停止")
                self._renew_authorization_session()
                final_url = self._authorize(email)
                self._require_signup_route(final_url, email)
                self._register_password(
                    email,
                    password,
                    retry_auth_step=False,
                    retry_account_creation=False,
                )
                return
            raise RuntimeError(f"提交注册密码失败: HTTP {response.status_code}: {response.text[:300]}")

    def _send_otp(self, referer: str = "") -> float:
        while not type(self)._otp_send_lock.acquire(timeout=0.5):
            self._check_stopped()
        try:
            delay = max(0.0, self._otp_send_interval - (time.monotonic() - type(self)._otp_last_sent_at))
            if delay > 0:
                self._log(f"验证码发送排队，等待 {delay:.1f} 秒")
                if self.stop_event.wait(delay):
                    raise RuntimeError("任务已停止")
            self._check_stopped()
            self._log("发送邮箱验证码")
            sent_at = time.time()
            type(self)._otp_last_sent_at = time.monotonic()
            response = self._request(
                "GET",
                f"{AUTH_BASE}/api/accounts/email-otp/send",
                headers=self._navigate_headers(referer or f"{AUTH_BASE}/create-account/password"),
                allow_redirects=True,
                verify=False,
            )
        finally:
            type(self)._otp_send_lock.release()
        if _cloudflare_challenge(response):
            target_url = str(getattr(response, "url", "") or f"{AUTH_BASE}/email-verification")
            if self._solve_cloudflare(target_url, reason="验证码请求遇到 Cloudflare 验证页", force_refresh=True):
                self._log("FlareSolverr 会话已导入，重新发送邮箱验证码")
                return self._send_otp(referer)
            raise CloudflareChallengeError("验证码请求被 Cloudflare 验证页拦截，未向邮箱投递")
        if response.status_code not in (200, 302):
            raise RuntimeError(f"发送验证码失败: HTTP {response.status_code}: {response.text[:240]}")
        self._log(f"验证码接口响应正常 (HTTP {response.status_code})，开始等待邮箱投递")
        return sent_at

    def _send_login_otp(self) -> float:
        while not type(self)._otp_send_lock.acquire(timeout=0.5):
            self._check_stopped()
        try:
            delay = max(0.0, self._otp_send_interval - (time.monotonic() - type(self)._otp_last_sent_at))
            if delay > 0 and self.stop_event.wait(delay):
                raise RuntimeError("任务已停止")
            self._check_stopped()
            self._log("已有账号改走邮箱验证码登录")
            sent_at = time.time()
            type(self)._otp_last_sent_at = time.monotonic()
            response = self._request(
                "POST",
                f"{AUTH_BASE}/api/accounts/passwordless/send-otp",
                headers=self._api_headers(f"{AUTH_BASE}/log-in/password"),
                verify=False,
            )
        finally:
            type(self)._otp_send_lock.release()
        if response.status_code != 200:
            raise RuntimeError(f"发送登录验证码失败: HTTP {response.status_code}: {response.text[:240]}")
        self._log("登录验证码已发送，开始等待邮箱投递")
        return sent_at

    def _wait_for_mail_code(
        self,
        mail: Any,
        mailbox: dict[str, Any],
        sent_at: float,
        *,
        excluded_codes: set[str] | None = None,
        label: str = "邮箱验证码",
    ) -> str:
        code = mail.wait_for_code(
            mailbox,
            requested_at=sent_at,
            timeout=max(30.0, float(self.registration.get("mail_wait_timeout") or 120)),
            interval=max(0.5, float(self.registration.get("mail_poll_interval") or 3)),
            stopped=self.stop_event.is_set,
            excluded_codes=excluded_codes or set(),
            on_status=self._log,
        )
        if not code:
            raise RuntimeError(f"等待{label}超时")
        return code

    def _login_existing_with_otp(
        self,
        email: str,
        mail: Any,
        mailbox: dict[str, Any],
        full_name: str,
        birthdate: str,
    ) -> dict[str, Any]:
        existing_codes = getattr(mail, "existing_codes", None)
        try:
            ignored_codes = set(existing_codes(mailbox)) if callable(existing_codes) else set()
        except Exception as exc:
            ignored_codes = set()
            self._log(f"读取旧验证码失败，继续发送新的登录验证码: {str(exc)[:180]}", "warning")
        sent_at = self._send_login_otp()
        code = self._wait_for_mail_code(
            mail,
            mailbox,
            sent_at,
            excluded_codes=ignored_codes,
            label="已有账号登录验证码",
        )
        validated = self._validate_otp(code)
        authorization_code = _response_code(validated)
        next_url = _continue_url(validated)
        if not authorization_code and urlparse(next_url).path.rstrip("/").lower() == "/about-you":
            self._log("已有账号仍需补全资料，正在完成账号资料")
            authorization_code = self._create_account(full_name, birthdate)
        if not authorization_code:
            authorization_code = self._read_authorization_code()
        if not authorization_code:
            raise RuntimeError(f"已有账号验证码登录完成，但 OAuth 未返回授权码: {email}")
        tokens = self._exchange_token(authorization_code)
        self._log(f"已有账号邮箱验证码登录成功: {email}", "success")
        return tokens

    def _validate_otp(self, code: str) -> Any:
        self._log("校验邮箱验证码")
        headers = self._api_headers(f"{AUTH_BASE}/email-verification")
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=headers,
            verify=False,
        )
        if self._wrong_otp_response(response):
            raise WrongOtpError("邮箱验证码已过期或不匹配")
        if response.status_code != 200:
            self._add_sentinel_headers(headers, "authorize_continue")
            response = self._request(
                "POST",
                f"{AUTH_BASE}/api/accounts/email-otp/validate",
                json={"code": code},
                headers=headers,
                verify=False,
            )
        if self._wrong_otp_response(response):
            raise WrongOtpError("邮箱验证码已过期或不匹配")
        if response.status_code != 200:
            raise RuntimeError(f"验证码校验失败: HTTP {response.status_code}: {response.text[:300]}")
        return response

    @staticmethod
    def _wrong_otp_response(response: Any) -> bool:
        if int(getattr(response, "status_code", 0) or 0) == 200:
            return False
        data = _json(response)
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = str(error.get("code") or data.get("code") or "").strip().lower()
        text = str(getattr(response, "text", "") or "").lower()
        return code == "wrong_email_otp_code" or "wrong_email_otp_code" in text

    def _read_authorization_code(self) -> str:
        response = self._request(
            "GET",
            self.authorize_url,
            headers=self._navigate_headers(f"{PLATFORM_BASE}/"),
            allow_redirects=True,
            verify=False,
        )
        return _response_code(response)

    def _create_account(self, full_name: str, birthdate: str) -> str:
        self._log("创建账号资料")
        headers = self._api_headers(f"{AUTH_BASE}/about-you")
        self._add_sentinel_headers(headers, "oauth_create_account")
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/create_account",
            json={"name": full_name, "birthdate": birthdate},
            headers=headers,
            verify=False,
        )
        if response.status_code not in (200, 302):
            data = _json(response)
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or data.get("code") or "").strip()
            message = str(error.get("message") or data.get("message") or response.text[:300]).strip()
            lowered = f"{code} {message}".lower()
            if str(code).strip().lower() == "registration_disallowed":
                raise RegistrationDisallowedError(
                    "OpenAI 拒绝创建账号资料 (registration_disallowed)：邮箱验证码已通过，当前注册上下文未获通过"
                )
            if any(marker in lowered for marker in ("deactivated", "disabled", "suspended", "terminated", "banned")):
                raise AccountBannedError(message or code or "账号已被封禁")
            raise RuntimeError(f"创建账号资料失败: HTTP {response.status_code}: {response.text[:300]}")
        code = _response_code(response)
        if code:
            return code
        code = self._read_authorization_code()
        if not code:
            raise RuntimeError("账号创建成功，但 OAuth 回调未返回授权码")
        return code

    def _exchange_token(self, code: str) -> dict[str, Any]:
        self._log("换取 Access Token 和 Refresh Token")
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "auth0-client": AUTH0_CLIENT,
            "content-type": "application/json",
            "origin": PLATFORM_BASE,
            "referer": f"{PLATFORM_BASE}/",
            "user-agent": self.user_agent,
            **self._client_hints(),
        }
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": CLIENT_ID,
                "code_verifier": self.code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            verify=False,
        )
        data = _json(response)
        if response.status_code != 200 or not str(data.get("access_token") or "").strip():
            raise RuntimeError(f"Token 换取失败: HTTP {response.status_code}: {response.text[:300]}")
        return data

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        token = str(refresh_token or "").strip()
        if not token:
            raise ValueError("账号缺少 Refresh Token")
        self._check_stopped()
        self._log("使用 Refresh Token 换取新 Token")
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "auth0-client": AUTH0_CLIENT,
            "cache-control": "no-cache",
            "content-type": "application/json",
            "origin": PLATFORM_BASE,
            "pragma": "no-cache",
            "referer": f"{PLATFORM_BASE}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": self.user_agent,
            **self._client_hints(),
        }
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/oauth/token",
            headers=headers,
            json={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
            verify=False,
        )
        data = _json(response)
        if response.status_code != 200:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or data.get("code") or "").strip()
            detail = str(error.get("message") or data.get("message") or response.text[:300]).strip()
            raise RuntimeError(f"Refresh Token 刷新失败: HTTP {response.status_code}: {code} {detail}".strip())
        if not str(data.get("access_token") or "").strip():
            raise RuntimeError("Refresh Token 刷新响应缺少 Access Token")
        if not str(data.get("refresh_token") or "").strip():
            data["refresh_token"] = token
        self._log("Refresh Token 刷新成功", "success")
        return data

    def relogin(
        self,
        email: str,
        password: str,
        *,
        mail_provider: str = "",
        _session_retry: bool = True,
    ) -> dict[str, Any]:
        email = str(email or "").strip()
        password = str(password or "").strip()
        if not email or not password:
            raise ValueError("账号缺少邮箱或密码")
        self._log(f"开始密码恢复登录: {email}")
        self._authorize(email, "login")
        headers = self._api_headers(f"{AUTH_BASE}/email-verification")
        self._add_sentinel_headers(headers, "password_verify")
        response = self._request(
            "POST",
            f"{AUTH_BASE}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
            verify=False,
        )
        data = _json(response)
        if response.status_code != 200:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(error.get("code") or data.get("code") or "").strip()
            message = str(error.get("message") or data.get("message") or response.text[:300]).strip()
            lowered = f"{code} {message}".lower()
            if any(marker in lowered for marker in ("deactivated", "disabled", "suspended", "terminated", "banned")):
                raise AccountBannedError(message or code or "账号已被封禁")
            if str(code).strip().lower() == "invalid_state" and _session_retry:
                self._log("登录授权会话已失效，正在重建会话后重试", "warning")
                self._renew_authorization_session()
                return self.relogin(email, password, mail_provider=mail_provider, _session_retry=False)
            raise RuntimeError(f"密码登录失败: HTTP {response.status_code}: {message or code}")

        authorization_code = _response_code(response)
        if not authorization_code:
            page = data.get("page") if isinstance(data.get("page"), dict) else {}
            page_type = str(page.get("type") or "").strip().lower()
            if page_type == "email_otp_verification":
                mail = self._mail_client(mail_provider)
                try:
                    mailbox = mail.existing_mailbox(email)
                    ignored_codes = mail.existing_codes(mailbox)
                    sent_at = self._send_otp(f"{AUTH_BASE}/email-verification")
                    self._log("等待登录验证码")
                    wait_timeout = max(30.0, float(self.registration.get("mail_wait_timeout") or 120))
                    wait_deadline = time.monotonic() + wait_timeout
                    validated = None
                    resend_count = 0
                    max_resends = 3
                    while validated is None and time.monotonic() < wait_deadline:
                        remaining = max(1.0, wait_deadline - time.monotonic())
                        otp = mail.wait_for_code(
                            mailbox,
                            requested_at=sent_at,
                            timeout=min(20.0, remaining),
                            interval=max(0.5, float(self.registration.get("mail_poll_interval") or 3)),
                            stopped=self.stop_event.is_set,
                            excluded_codes=ignored_codes,
                            on_status=self._log,
                        )
                        if not otp:
                            if resend_count >= max_resends or time.monotonic() >= wait_deadline:
                                raise RuntimeError("等待登录验证码超时")
                            sent_at = self._send_otp(f"{AUTH_BASE}/email-verification")
                            resend_count += 1
                            self._log(f"暂未收到最新验证码，已重新发送 ({resend_count}/{max_resends})", "warning")
                            continue
                        try:
                            validated = self._validate_otp(otp)
                        except WrongOtpError:
                            ignored_codes.add(otp)
                            if resend_count < max_resends and time.monotonic() < wait_deadline:
                                sent_at = self._send_otp(f"{AUTH_BASE}/email-verification")
                                resend_count += 1
                                self._log(f"验证码已失效，已重新发送 ({resend_count}/{max_resends})", "warning")
                            else:
                                self._log("验证码已失效，继续等待最新验证码", "warning")
                    if validated is None:
                        raise RuntimeError("等待有效登录验证码超时")
                    authorization_code = _response_code(validated)
                    validated_data = _json(validated)
                    validated_page = validated_data.get("page") if isinstance(validated_data.get("page"), dict) else {}
                    validated_page_type = str(validated_page.get("type") or "").strip().lower().replace("-", "_")
                    if not authorization_code and validated_page_type == "about_you":
                        self._log("登录账号需要补全资料，正在完成账号资料")
                        authorization_code = self._create_account("ChatGPT User", "2000-01-01")
                finally:
                    mail.close()
            if not authorization_code:
                authorization_code = self._read_authorization_code()
        if not authorization_code:
            if _session_retry:
                self._log("验证码校验后未取得授权码，正在重建会话后完整重登", "warning")
                self._renew_authorization_session()
                try:
                    return self.relogin(email, password, mail_provider=mail_provider, _session_retry=False)
                except Exception as exc:
                    raise RuntimeError(f"首次登录未取得授权码，重建会话后重试失败: {str(exc)[:300]}") from exc
            raise RuntimeError("密码登录完成，但 OAuth 授权码为空")

        self._log("换取新的 Access Token 和 Refresh Token")
        tokens = self._exchange_token(authorization_code)
        self._log(f"密码恢复登录成功: {email}", "success")
        return {
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "token_type": str(tokens.get("token_type") or "Bearer"),
            "expires_in": int(tokens.get("expires_in") or 0),
        }

    def register(self) -> dict[str, Any]:
        self._check_stopped()
        mail = self._mail_client()
        mailbox: dict[str, Any] | None = None
        mailbox_committed = False
        try:
            source = str(getattr(mail, "provider_name", "yyds"))
            self._log("领取 Outlook 邮箱" if source == "outlook" else "创建临时邮箱")
            mailbox = mail.create_mailbox()
            email = mailbox["address"]
            if source == "outlook":
                split_index = int(mailbox.get("split_index") or 0)
                if split_index == 0:
                    self._log(f"Outlook 母号就绪: {email}", "success")
                else:
                    self._log(f"Outlook 分裂号 #{split_index} 就绪: {email}", "success")
            self._log(f"邮箱就绪: {email}", "success")
            password = random_password()
            first_name = random.choice(("James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"))
            last_name = random.choice(("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"))
            birthdate = f"{random.randint(1996, 2004):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"

            full_name = f"{first_name} {last_name}"
            final_url = self._authorize(email)
            route = _authorization_route(final_url)
            registration_mode = "signup"
            account_password = password
            if route == "existing":
                if source != "outlook":
                    raise ExistingAccountRouteError(f"邮箱已存在，OAuth 已进入登录步骤: {email}")
                self._log("Outlook 地址已注册，自动切换到邮箱验证码登录", "warning")
                commit_mailbox = getattr(mail, "commit_mailbox", None)
                if callable(commit_mailbox):
                    commit_mailbox(mailbox)
                    mailbox_committed = True
                    self._log("已有 Outlook 地址已登记为占用，不再重复注册", "success")
                tokens = self._login_existing_with_otp(email, mail, mailbox, full_name, birthdate)
                registration_mode = "existing_otp"
                account_password = ""
            else:
                self._require_signup_route(final_url, email)
                self._register_password(email, password)
                sent_at = self._send_otp()
                self._log("等待邮箱验证码")
                code = self._wait_for_mail_code(mail, mailbox, sent_at)
                self._validate_otp(code)
                authorization_code = self._create_account(full_name, birthdate)
                tokens = self._exchange_token(authorization_code)
            commit_mailbox = getattr(mail, "commit_mailbox", None)
            if callable(commit_mailbox) and not mailbox_committed:
                commit_mailbox(mailbox)
                mailbox_committed = True
                self._log("Outlook 邮箱已登记为已使用", "success")
            access_token = str(tokens.get("access_token") or "").strip()
            refresh_token = str(tokens.get("refresh_token") or "").strip()
            id_token = str(tokens.get("id_token") or "").strip()
            claims = _jwt_claims(id_token or access_token)
            now = datetime.now(timezone.utc).isoformat()
            result = {
                "id": str(claims.get("sub") or uuid.uuid4().hex),
                "email": email,
                "password": account_password,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "token_type": str(tokens.get("token_type") or "Bearer"),
                "expires_in": int(tokens.get("expires_in") or 0),
                "source_type": "protocol",
                "mail_provider": source,
                "registration_mode": registration_mode,
                "created_at": now,
            }
            success_label = "已有账号登录成功" if registration_mode == "existing_otp" else "注册成功"
            self._log(f"{success_label}: {email}", "success")
            return result
        except Exception as exc:
            fail_mailbox = getattr(mail, "fail_mailbox", None)
            if mailbox is not None and callable(fail_mailbox) and outlook_error_should_disable(exc):
                fail_mailbox(mailbox, str(exc))
                self._log(
                    f"Outlook 母号已标记失效，后续不再注册：{mailbox.get('base_address') or mailbox.get('address')}，原因：{str(exc)[:180]}",
                    "warning",
                )
            elif mailbox is not None and getattr(mail, "provider_name", "") == "outlook":
                self._log(
                    f"Outlook 母号本次失败为环境/中断类，释放回号池：{mailbox.get('base_address') or mailbox.get('address')}，原因：{str(exc)[:180]}",
                    "warning",
                )
            raise
        finally:
            mail.close()
