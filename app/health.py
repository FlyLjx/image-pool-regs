from __future__ import annotations

import copy
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Callable
from urllib.parse import urlparse

from curl_cffi import requests

from app.manager import RegistrationManager
from app.registration.protocol import (
    AccountBannedError,
    CHROME_UA,
    SAFARI_UA,
    ProtocolRegistrar,
    flaresolverr_proxy_url,
)
from app.storage import DEFAULT_SETTINGS, JsonStore, deep_merge
from app.time_utils import iso_now


HEALTH_URL = "https://chatgpt.com/backend-api/me"
BANNED_MARKERS = (
    "account_deactivated",
    "account disabled",
    "account is disabled",
    "account suspended",
    "account is suspended",
    "account terminated",
    "account is terminated",
    "account banned",
    "account is banned",
)
CHALLENGE_MARKERS = (
    "enable javascript and cookies to continue",
    "_cf_chl_opt",
    "__cf_chl_tk",
    "challenges.cloudflare.com",
    "<title>just a moment",
)


def _now() -> str:
    return iso_now()


def _elapsed_seconds(started_at: Any, ended_at: Any) -> int:
    try:
        start = datetime.fromisoformat(str(started_at or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at or "").replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return 0


def cloudflare_challenge(status_code: int, response_text: str = "", headers: dict[str, Any] | None = None) -> bool:
    status = int(status_code or 0)
    content = str(response_text or "").lower()
    if any(marker in content for marker in CHALLENGE_MARKERS):
        return True
    response_headers = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    return (
        status in {403, 429, 503}
        and "cloudflare" in response_headers.get("server", "")
        and "text/html" in response_headers.get("content-type", "")
    )


def classify_health(status_code: int, response_text: str = "") -> dict[str, Any]:
    status = int(status_code or 0)
    content = str(response_text or "").lower()
    if any(marker in content for marker in BANNED_MARKERS):
        return {"status": "banned", "alive": False, "confirmed_ban": True, "detail": "账号已被明确封禁"}
    if cloudflare_challenge(status, content):
        return {
            "status": "environment",
            "alive": False,
            "confirmed_ban": False,
            "detail": "检测环境触发 Cloudflare 验证，账号状态未判定",
        }
    if 200 <= status < 300:
        return {"status": "alive", "alive": True, "confirmed_ban": False, "detail": "账号接口响应正常"}
    if status == 401:
        return {"status": "expired", "alive": False, "confirmed_ban": False, "detail": "Access Token 已失效"}
    if status == 403:
        return {"status": "restricted", "alive": False, "confirmed_ban": False, "detail": "账号访问受限"}
    if status == 429:
        return {"status": "rate_limited", "alive": False, "confirmed_ban": False, "detail": "账号接口限流"}
    if status >= 500:
        return {"status": "unknown", "alive": False, "confirmed_ban": False, "detail": f"上游 HTTP {status}"}
    return {"status": "unknown", "alive": False, "confirmed_ban": False, "detail": f"账号接口 HTTP {status}"}


class AccountHealthChecker:
    _flare_lock = threading.Lock()
    _flare_cache: dict[str, dict[str, Any]] = {}
    _flare_cache_seconds = 1200.0

    def __init__(
        self,
        settings: dict[str, Any],
        logger: Callable[[str, str], None] | None = None,
        stop_event: threading.Event | None = None,
        registrar_factory: Callable[..., ProtocolRegistrar] = ProtocolRegistrar,
    ) -> None:
        self.settings = settings
        self.registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        self.health = settings.get("health") if isinstance(settings.get("health"), dict) else {}
        self.flaresolverr = settings.get("flaresolverr") if isinstance(settings.get("flaresolverr"), dict) else {}
        self.proxy = str(self.registration.get("proxy") or "").strip()
        self.logger = logger or (lambda _level, _message: None)
        self.stop_event = stop_event or threading.Event()
        self.registrar_factory = registrar_factory

    def _new_session(self, *, use_proxy: bool = True) -> Any:
        profile = str(self.registration.get("browser_profile") or "chrome_windows")
        options: dict[str, Any] = {
            "impersonate": "safari180" if profile == "safari_macos" else "chrome145",
            "verify": False,
        }
        if use_proxy and self.proxy:
            options["proxy"] = self.proxy
        return requests.Session(**options)

    def _health_request(self, session: Any, access_token: str, user_agent: str = "") -> Any:
        profile = str(self.registration.get("browser_profile") or "chrome_windows")
        is_safari = profile == "safari_macos"
        resolved_user_agent = str(user_agent or (SAFARI_UA if is_safari else CHROME_UA))
        headers = {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "authorization": f"Bearer {access_token}",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "user-agent": resolved_user_agent,
        }
        if not is_safari:
            version_match = re.search(r"(?:Chrome|Chromium)/([\d.]+)", resolved_user_agent)
            full_version = version_match.group(1) if version_match else "145.0.0.0"
            major_version = full_version.split(".", 1)[0]
            platform = "Linux" if "Linux" in resolved_user_agent else "Windows"
            headers.update({
                "sec-ch-ua": f'"Google Chrome";v="{major_version}", "Not?A_Brand";v="8", "Chromium";v="{major_version}"',
                "sec-ch-ua-full-version-list": (
                    f'"Chromium";v="{full_version}", "Not:A-Brand";v="99.0.0.0", '
                    f'"Google Chrome";v="{full_version}"'
                ),
                "sec-ch-ua-platform": f'"{platform}"',
                "sec-ch-ua-platform-version": '"10.0.0"' if platform == "Windows" else '"6.0.0"',
            })
        return session.get(
            HEALTH_URL,
            headers=headers,
            timeout=max(10.0, float(self.health.get("request_timeout") or 30)),
            verify=False,
        )

    def _flaresolverr_endpoints(self) -> list[str]:
        api_url = str(self.flaresolverr.get("url") or "").strip().rstrip("/")
        if not api_url:
            return []
        endpoints = [api_url if api_url.endswith("/v1") else f"{api_url}/v1"]
        parsed = urlparse(api_url)
        if parsed.hostname == "flaresolverr":
            endpoints.append(f"http://127.0.0.1:{parsed.port or 8191}/v1")
        return list(dict.fromkeys(endpoints))

    def _flare_cache_key(self) -> str:
        proxy_key = self.proxy if bool(self.flaresolverr.get("pass_proxy", True)) else "direct"
        return f"{'|'.join(self._flaresolverr_endpoints())}|{proxy_key}"

    def _cached_flare_solution(self) -> dict[str, Any] | None:
        cached = type(self)._flare_cache.get(self._flare_cache_key())
        if not cached or time.monotonic() - float(cached.get("cached_at") or 0) >= self._flare_cache_seconds:
            return None
        return cached

    def _solve_cloudflare(self) -> dict[str, Any]:
        endpoints = self._flaresolverr_endpoints()
        if not bool(self.flaresolverr.get("enabled")) or not endpoints:
            raise RuntimeError("FlareSolverr 未启用或未配置地址")
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": HEALTH_URL,
            "maxTimeout": max(1000, int(self.flaresolverr.get("max_timeout_ms") or 60000)),
        }
        if self.proxy and bool(self.flaresolverr.get("pass_proxy", True)):
            payload["proxy"] = {"url": flaresolverr_proxy_url(self.proxy)}
        self.logger("warning", "账号检测遇到 Cloudflare 验证页，启动 FlareSolverr")
        errors: list[str] = []
        for endpoint in endpoints:
            try:
                response = requests.post(
                    endpoint,
                    json=payload,
                    timeout=max(15, payload["maxTimeout"] / 1000 + 10),
                    verify=False,
                )
                data = response.json() if response.text else {}
            except Exception as exc:
                errors.append(f"{endpoint}: {str(exc)[:180]}")
                continue
            solution = data.get("solution") if isinstance(data, dict) and isinstance(data.get("solution"), dict) else {}
            if response.status_code == 200 and str(data.get("status") or "").lower() == "ok" and solution:
                cached = copy.deepcopy(solution)
                cached["cached_at"] = time.monotonic()
                self.logger("info", f"FlareSolverr 验证完成，取得 {len(solution.get('cookies') or [])} 个 Cookie")
                return cached
            errors.append(f"{endpoint}: HTTP {response.status_code}: {str(response.text or '')[:180]}")
        raise RuntimeError(f"FlareSolverr 请求失败: {'; '.join(errors)[-600:]}")

    @staticmethod
    def _apply_flare_solution(session: Any, solution: dict[str, Any] | None) -> str:
        if not solution:
            return ""
        for item in solution.get("cookies") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            session.cookies.set(
                str(item["name"]),
                str(item.get("value") or ""),
                domain=str(item.get("domain") or ".chatgpt.com"),
                path=str(item.get("path") or "/"),
            )
        return str(solution.get("userAgent") or "").strip()

    def _probe(self, access_token: str) -> tuple[int, str]:
        if self.stop_event.is_set():
            raise RuntimeError("账号检测已停止")
        initial_solution = self._cached_flare_solution()
        pass_proxy = bool(self.flaresolverr.get("pass_proxy", True))
        session = self._new_session(use_proxy=not initial_solution or pass_proxy)
        try:
            user_agent = self._apply_flare_solution(session, initial_solution)
            response = self._health_request(session, access_token, user_agent)
            if cloudflare_challenge(response.status_code, response.text, response.headers):
                with type(self)._flare_lock:
                    solution = self._cached_flare_solution()
                    if initial_solution and solution is initial_solution:
                        type(self)._flare_cache.pop(self._flare_cache_key(), None)
                        solution = None
                    if solution is None:
                        try:
                            solution = self._solve_cloudflare()
                            type(self)._flare_cache[self._flare_cache_key()] = solution
                        except Exception as exc:
                            self.logger("warning", f"账号检测 FlareSolverr 兜底失败: {str(exc)[:300]}")
                            solution = None
                if solution:
                    session.close()
                    session = self._new_session(use_proxy=pass_proxy)
                    user_agent = self._apply_flare_solution(session, solution)
                    response = self._health_request(session, access_token, user_agent)
                    if cloudflare_challenge(response.status_code, response.text, response.headers):
                        self.logger("warning", "FlareSolverr Cookie 已导入，但账号检测仍遇到 Cloudflare 验证页")
            return int(response.status_code or 0), str(response.text or "")[:20000]
        finally:
            session.close()

    @staticmethod
    def _updates(result: dict[str, Any], *, http_status: int, recovery_status: str = "") -> dict[str, Any]:
        status = str(result.get("status") or "unknown")
        updates = {
            "health_status": status,
            "health_alive": bool(result.get("alive")),
            "health_http_status": int(http_status or 0),
            "health_checked_at": _now(),
            "health_detail": str(result.get("detail") or "")[:500],
            "disabled": status == "banned",
        }
        if recovery_status:
            updates["health_recovery_status"] = recovery_status
            updates["health_recovery_updated_at"] = updates["health_checked_at"]
        if status == "banned":
            updates["disabled_reason"] = updates["health_detail"]
        elif status == "alive":
            updates["disabled_reason"] = ""
        return updates

    def probe(self, account: dict[str, Any]) -> dict[str, Any]:
        if str(account.get("health_status") or "").lower() == "banned" and bool(account.get("disabled")):
            result = {"status": "banned", "alive": False, "detail": "账号已标记封禁", "confirmed_ban": True}
            return {
                "updates": self._updates(result, http_status=int(account.get("health_http_status") or 0)),
                "recovered": False,
                "needs_recovery": False,
            }

        access_token = str(account.get("access_token") or "").strip()
        if access_token:
            try:
                http_status, response_text = self._probe(access_token)
            except Exception as exc:
                result = {"status": "unknown", "alive": False, "detail": f"检测请求失败: {str(exc)[:300]}"}
                return {"updates": self._updates(result, http_status=0), "recovered": False, "needs_recovery": False}
            result = classify_health(http_status, response_text)
        else:
            http_status = 401
            result = {"status": "expired", "alive": False, "confirmed_ban": False, "detail": "账号缺少 Access Token"}

        return {
            "updates": self._updates(result, http_status=http_status),
            "recovered": False,
            "needs_recovery": result["status"] == "expired",
            "initial_result": result,
            "initial_http_status": http_status,
        }

    def recover(self, account: dict[str, Any], probe_result: dict[str, Any]) -> dict[str, Any]:
        result = probe_result.get("initial_result") if isinstance(probe_result.get("initial_result"), dict) else {
            "status": "expired",
            "alive": False,
            "confirmed_ban": False,
            "detail": "Access Token 已失效",
        }
        http_status = int(probe_result.get("initial_http_status") or 401)

        refresh_token = str(account.get("refresh_token") or "").strip()
        refresh_error = ""
        registrar = self.registrar_factory(self.settings, logger=self.logger, stop_event=self.stop_event)
        try:
            refresh = getattr(registrar, "refresh", None)
            if refresh_token and callable(refresh):
                try:
                    refreshed_tokens = refresh(refresh_token)
                    refreshed_result = self._verify_recovery_tokens(refreshed_tokens)
                    if str(refreshed_result.get("verified_status") or "") != "expired":
                        return refreshed_result
                    refresh_error = "Refresh Token 换取的新 Token 仍然失效"
                except Exception as exc:
                    refresh_error = str(exc)[:350]
                    self.logger("warning", f"Refresh Token 恢复失败，准备密码重登: {refresh_error}")
        except AccountBannedError as exc:
            banned = {"status": "banned", "alive": False, "confirmed_ban": True, "detail": str(exc)[:500]}
            return {"updates": self._updates(banned, http_status=http_status, recovery_status="banned"), "recovered": False}
        finally:
            registrar.close()

        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "").strip()
        if not email or not password:
            updates = self._updates(result, http_status=http_status, recovery_status="missing_credentials")
            updates["health_detail"] = "Token 恢复失败，账号缺少邮箱或密码"
            if refresh_error:
                updates["health_detail"] += f": {refresh_error}"
            return {"updates": updates, "recovered": False}

        registrar = self.registrar_factory(self.settings, logger=self.logger, stop_event=self.stop_event)
        try:
            mail_provider = str(account.get("mail_provider") or "").strip().lower()
            if mail_provider == "outlook":
                tokens = registrar.relogin(email, password, mail_provider=mail_provider)
            else:
                tokens = registrar.relogin(email, password)
        except AccountBannedError as exc:
            banned = {"status": "banned", "alive": False, "confirmed_ban": True, "detail": str(exc)[:500]}
            return {"updates": self._updates(banned, http_status=http_status, recovery_status="banned"), "recovered": False}
        except Exception as exc:
            updates = self._updates(result, http_status=http_status, recovery_status="failed")
            prefix = f"Refresh Token 失败: {refresh_error}; " if refresh_error else ""
            updates["health_detail"] = f"{prefix}密码恢复失败: {str(exc)[:350]}"[:500]
            return {"updates": updates, "recovered": False}
        finally:
            registrar.close()

        return self._verify_recovery_tokens(tokens)

    def _verify_recovery_tokens(self, tokens: dict[str, Any]) -> dict[str, Any]:
        new_access_token = str(tokens.get("access_token") or "").strip()
        self.logger("info", "验证刷新后的 Token")
        try:
            verify_status, verify_text = self._probe(new_access_token)
            verified = classify_health(verify_status, verify_text)
        except Exception as exc:
            verify_status = 0
            verified = {"status": "unknown", "alive": False, "detail": f"新 Token 验证失败: {str(exc)[:300]}"}
        updates = self._updates(
            verified,
            http_status=verify_status,
            recovery_status="recovered" if verified.get("alive") else "token_refreshed",
        )
        for key in ("access_token", "refresh_token", "id_token", "token_type", "expires_in"):
            value = tokens.get(key)
            if value not in (None, ""):
                updates[key] = value
        updates["token_refreshed_at"] = _now()
        return {
            "updates": updates,
            "recovered": bool(verified.get("alive")),
            "needs_recovery": False,
            "verified_status": str(verified.get("status") or "unknown"),
        }

    def check(self, account: dict[str, Any]) -> dict[str, Any]:
        probe_result = self.probe(account)
        if not probe_result.get("needs_recovery"):
            return probe_result
        return self.recover(account, probe_result)


class AccountHealthService:
    def __init__(
        self,
        store: JsonStore,
        manager: RegistrationManager,
        checker_factory: Callable[..., AccountHealthChecker] = AccountHealthChecker,
        minimum_interval: float = 60.0,
    ) -> None:
        self.store = store
        self.manager = manager
        self.checker_factory = checker_factory
        self.minimum_interval = max(0.01, float(minimum_interval))
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._batch_cancel = threading.Event()
        self._wake = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._batch_thread: threading.Thread | None = None
        self._batch_queue: Queue[dict[str, Any]] = Queue()
        self._batch_accounts: list[dict[str, Any]] = []
        self._batch_known_ids: set[str] = set()
        self._batch_accepting = False
        self._recovery_progress: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {
            "auto_enabled": False,
            "state": "idle",
            "source": "",
            "total": 0,
            "probed": 0,
            "checked": 0,
            "alive": 0,
            "recovered": 0,
            "recovery_total": 0,
            "recovery_completed": 0,
            "recovery_running": 0,
            "check_concurrency": 0,
            "recovery_concurrency": 0,
            "banned": 0,
            "expired": 0,
            "failed": 0,
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "current_email": "",
            "message": "等待检测",
        }

    def start(self) -> None:
        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._clear_stale_statuses()
            self._shutdown.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, name="account-health-monitor", daemon=True)
            self._monitor_thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._batch_cancel.set()
        self._wake.set()
        for thread in (self._monitor_thread, self._batch_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._state)
            result["running"] = bool(self._batch_thread and self._batch_thread.is_alive())
            result["thread_alive"] = bool(self._monitor_thread and self._monitor_thread.is_alive())
            recovery_pending = max(0, int(result.get("recovery_total") or 0) - int(result.get("recovery_completed") or 0))
            result["recovery_pending"] = recovery_pending
            result["recovery_active"] = max(0, int(result.get("recovery_running") or 0)) if result["running"] else 0
            result["recovery_waiting"] = max(0, recovery_pending - result["recovery_active"])
            recovery_items = sorted(
                (copy.deepcopy(item) for item in self._recovery_progress.values()),
                key=lambda item: str(item.get("updated_at") or ""),
                reverse=True,
            )
            stage_counts: dict[str, int] = {}
            for item in recovery_items:
                stage = str(item.get("stage") or "starting")
                stage_counts[stage] = stage_counts.get(stage, 0) + 1
            result["recovery_items"] = recovery_items
            result["recovery_stage_counts"] = stage_counts
        return result

    def set_auto_enabled(self, enabled: bool) -> dict[str, Any]:
        def update(raw: Any) -> dict[str, Any]:
            settings = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
            settings.setdefault("health", {})["auto_check_enabled"] = bool(enabled)
            return settings

        self.store.update("settings.json", DEFAULT_SETTINGS, update)
        with self._lock:
            self._state["auto_enabled"] = bool(enabled)
        self.wake()
        return self.status()

    def start_check(self, account_ids: list[str] | None = None, source: str = "manual") -> dict[str, Any]:
        targets = {str(value).strip() for value in (account_ids or []) if str(value).strip()}
        raw = self.store.read("accounts.json", [])
        accounts = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        if targets:
            accounts = [item for item in accounts if str(item.get("id") or "").strip() in targets]
        accounts = [
            item for item in accounts
            if str(item.get("provider") or "openai").strip().lower() == "openai"
        ]
        timestamp = _now()
        added = 0
        with self._lock:
            batch_running = bool(self._batch_thread and self._batch_thread.is_alive())
            if batch_running:
                if not self._batch_accepting or self._batch_cancel.is_set():
                    raise RuntimeError("账号检测任务正在停止或收尾")
                pending = [
                    item for item in accounts
                    if str(item.get("id") or "").strip() not in self._batch_known_ids
                ]
                if pending:
                    self._mark_checking(pending, timestamp)
                    for item in pending:
                        account_id = str(item.get("id") or "").strip()
                        self._batch_known_ids.add(account_id)
                        self._batch_accounts.append(item)
                        self._batch_queue.put(item)
                    added = len(pending)
                    self._state["total"] += added
                    self._state["message"] = f"已追加 {added} 个账号，正在并发检测"
                result = self.status()
                if added:
                    self.manager.log("info", f"账号检测追加：{added} 个，并发执行")
                return result

            self._batch_cancel.clear()
            self._batch_queue = Queue()
            self._batch_accounts = list(accounts)
            self._batch_known_ids = {str(item.get("id") or "").strip() for item in accounts}
            self._batch_accepting = bool(accounts)
            self._recovery_progress = {}
            for item in accounts:
                self._batch_queue.put(item)
            self._state.update({
                "state": "running" if accounts else "completed",
                "source": source,
                "total": len(accounts),
                "probed": 0,
                "checked": 0,
                "alive": 0,
                "recovered": 0,
                "recovery_total": 0,
                "recovery_completed": 0,
                "recovery_running": 0,
                "check_concurrency": 0,
                "recovery_concurrency": 0,
                "banned": 0,
                "expired": 0,
                "failed": 0,
                "started_at": timestamp,
                "finished_at": "" if accounts else timestamp,
                "last_error": "",
                "current_email": "",
                "message": "正在检测账号" if accounts else "没有可检测账号",
            })
            if accounts:
                self._mark_checking(accounts, timestamp)
                settings = self.store.settings()
                self._batch_thread = threading.Thread(
                    target=self._run_batch,
                    args=(settings,),
                    name="account-health-batch",
                    daemon=True,
                )
                self._batch_thread.start()
        if accounts:
            self.manager.log("info", f"账号检测启动：{len(accounts)} 个")
        return self.status()

    def stop_check(self) -> dict[str, Any]:
        with self._lock:
            active = bool(self._batch_thread and self._batch_thread.is_alive())
            if active:
                self._state["state"] = "stopping"
                self._state["message"] = "正在停止当前检测/恢复"
                self._batch_cancel.set()
        if active:
            self.manager.log("warning", "正在停止当前账号检测/恢复批次")
        return self.status()

    def _mark_checking(self, accounts: list[dict[str, Any]], timestamp: str) -> None:
        targets = {str(item.get("id") or "").strip() for item in accounts}

        def update(raw: Any) -> list[dict[str, Any]]:
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in items:
                account_id = str(item.get("id") or "").strip()
                if account_id not in targets:
                    continue
                if str(item.get("health_status") or "").lower() == "banned" and bool(item.get("disabled")):
                    continue
                item.update({
                    "health_status": "checking",
                    "health_alive": False,
                    "health_detail": "等待检测",
                    "health_check_started_at": timestamp,
                })
            return items

        self.store.update("accounts.json", [], update)

    def _clear_stale_statuses(self) -> None:
        def update(raw: Any) -> list[dict[str, Any]]:
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in items:
                if str(item.get("health_status") or "").lower() not in {"checking", "recovering"}:
                    continue
                item.update({
                    "health_status": "unchecked",
                    "health_alive": False,
                    "health_detail": "上次检测/恢复未完成，请重新检测",
                    "health_recovery_status": "interrupted",
                    "health_recovery_updated_at": _now(),
                })
            return items

        self.store.update("accounts.json", [], update)

    def _update_account(self, account: dict[str, Any], updates: dict[str, Any]) -> None:
        account_id = str(account.get("id") or "").strip()

        def update(raw: Any) -> list[dict[str, Any]]:
            accounts = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in accounts:
                if str(item.get("id") or "").strip() == account_id:
                    timestamp = str(updates.get("health_checked_at") or _now())
                    item.setdefault("survival_started_at", str(item.get("created_at") or timestamp))
                    item.setdefault("survival_ended_at", "")
                    item.setdefault("survival_last_seconds", 0)
                    item.setdefault("survival_total_seconds", 0)
                    item.setdefault("survival_recovery_count", 0)
                    item.setdefault("survival_interval_recorded", False)
                    health_status = str(updates.get("health_status") or "").lower()
                    recovery_status = str(updates.get("health_recovery_status") or "").lower()
                    ended_at = str(item.get("survival_ended_at") or "")
                    recorded = bool(item.get("survival_interval_recorded"))
                    if health_status in {"expired", "banned"}:
                        if not ended_at:
                            ended_at = timestamp
                            item["survival_ended_at"] = ended_at
                        if health_status == "banned" and not recorded:
                            elapsed = _elapsed_seconds(item.get("survival_started_at"), ended_at)
                            item["survival_last_seconds"] = elapsed
                            item["survival_total_seconds"] = max(0, int(item.get("survival_total_seconds") or 0)) + elapsed
                            item["survival_interval_recorded"] = True
                            item["disabled_at"] = str(item.get("disabled_at") or timestamp)
                    elif health_status == "alive" and (ended_at or recovery_status == "recovered"):
                        interval_end = ended_at or timestamp
                        elapsed = _elapsed_seconds(item.get("survival_started_at"), interval_end)
                        if not recorded:
                            item["survival_total_seconds"] = max(0, int(item.get("survival_total_seconds") or 0)) + elapsed
                        item["survival_last_seconds"] = elapsed
                        item["survival_started_at"] = timestamp
                        item["survival_ended_at"] = ""
                        item["survival_interval_recorded"] = False
                        if recovery_status == "recovered":
                            item["survival_recovery_count"] = max(0, int(item.get("survival_recovery_count") or 0)) + 1
                    item.update(copy.deepcopy(updates))
                    break
            return accounts

        self.store.update("accounts.json", [], update)

    def _new_checker(self, settings: dict[str, Any], recovery_account: dict[str, Any] | None = None) -> Any:
        def logger(level: str, message: str) -> None:
            self.manager.log(level, message)
            if recovery_account is not None and not self._batch_cancel.is_set():
                self._mark_recovery_progress(recovery_account, message)

        return self.checker_factory(
            settings,
            logger=logger,
            stop_event=self._batch_cancel,
        )

    def _probe_one(self, settings: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        if self._batch_cancel.is_set():
            raise RuntimeError("账号检测/恢复已停止")
        checker = self._new_checker(settings)
        probe = getattr(checker, "probe", None)
        return probe(account) if callable(probe) else checker.check(account)

    def _recover_one(self, settings: dict[str, Any], account: dict[str, Any], probe_result: dict[str, Any]) -> dict[str, Any]:
        if self._batch_cancel.is_set():
            raise RuntimeError("账号检测/恢复已停止")
        with self._lock:
            self._state["recovery_running"] += 1
        try:
            self._mark_recovery_progress(account, "恢复线程已启动")
            checker = self._new_checker(settings, recovery_account=account)
            recover = getattr(checker, "recover", None)
            return recover(account, probe_result) if callable(recover) else checker.check(account)
        finally:
            with self._lock:
                self._state["recovery_running"] = max(0, int(self._state.get("recovery_running") or 0) - 1)
                self._recovery_progress.pop(str(account.get("id") or "").strip(), None)

    @staticmethod
    def _failed_result(detail: str) -> dict[str, Any]:
        return {
            "updates": {
                "health_status": "unknown",
                "health_alive": False,
                "health_checked_at": _now(),
                "health_detail": str(detail)[:400],
            },
            "recovered": False,
            "needs_recovery": False,
        }

    def _record_result(self, account: dict[str, Any], result: dict[str, Any]) -> None:
        updates = result["updates"]
        status = str(updates.get("health_status") or "unknown")
        recovered = bool(result.get("recovered"))
        email = str(account.get("email") or "unknown")
        removed = 0
        if status == "banned":
            removed = self.manager.delete_accounts([str(account.get("id") or "")])
        else:
            self._update_account(account, updates)
        with self._lock:
            self._state["checked"] += 1
            self._state["current_email"] = email
            if recovered:
                self._state["recovered"] += 1
                self._state["alive"] += 1
            elif status == "alive":
                self._state["alive"] += 1
            elif status == "banned":
                self._state["banned"] += 1
            elif status == "expired":
                self._state["expired"] += 1
            else:
                self._state["failed"] += 1
        level = "success" if status == "alive" else "error" if status == "banned" else "warning"
        suffix = "，密码恢复成功，存活时间已重置" if recovered else ""
        if status == "banned" and removed:
            suffix = "，已从本地账号中自动删除"
        self.manager.log(level, f"账号检测：{email}，状态={status}{suffix}")

    def _record_result_safely(self, account: dict[str, Any], result: dict[str, Any]) -> None:
        try:
            self._record_result(account, result)
        except Exception as exc:
            email = str(account.get("email") or "unknown")
            with self._lock:
                self._state["checked"] += 1
                self._state["failed"] += 1
                self._state["current_email"] = email
                self._state["last_error"] = str(exc)[:400]
            self.manager.log("error", f"账号检测结果写入失败：{email}，{str(exc)[:300]}")

    def _mark_recovering(self, account: dict[str, Any]) -> None:
        self._update_account(account, {
            "health_status": "recovering",
            "health_alive": False,
            "health_checked_at": _now(),
            "health_detail": "Token 已失效，正在等待 Token 恢复",
            "health_recovery_status": "queued",
            "health_recovery_updated_at": _now(),
        })

    def _mark_recovery_progress(self, account: dict[str, Any], message: str) -> None:
        account_id = str(account.get("id") or "").strip()
        timestamp = _now()
        stage, stage_label = self._recovery_stage(message)

        def update(raw: Any) -> list[dict[str, Any]]:
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in items:
                if str(item.get("id") or "").strip() != account_id:
                    continue
                item.update({
                    "health_status": "recovering",
                    "health_alive": False,
                    "health_detail": str(message or "正在恢复账号")[:500],
                    "health_recovery_status": "running",
                    "health_recovery_updated_at": timestamp,
                })
                break
            return items

        self.store.update("accounts.json", [], update)
        with self._lock:
            self._recovery_progress[account_id] = {
                "id": account_id,
                "email": str(account.get("email") or ""),
                "stage": stage,
                "stage_label": stage_label,
                "message": str(message or stage_label)[:500],
                "updated_at": timestamp,
            }

    @staticmethod
    def _recovery_stage(message: str) -> tuple[str, str]:
        text = str(message or "").lower()
        if "flaresolverr" in text or "cloudflare" in text:
            return "challenge", "处理 Cloudflare"
        if "验证码" in text or "otp" in text:
            return "otp", "等待/校验验证码"
        if "补全资料" in text or "账号资料" in text:
            return "profile", "补全账号资料"
        if "验证刷新" in text or "验证新 token" in text:
            return "verify", "验证新 Token"
        if "refresh token" in text:
            return "refresh", "刷新 Token"
        if "换取" in text and "token" in text:
            return "token", "换取新 Token"
        if "oauth" in text or "授权会话" in text:
            return "oauth", "初始化 OAuth"
        if "密码恢复登录" in text or "提交密码" in text:
            return "password", "提交密码登录"
        return "starting", "启动恢复"

    def _mark_batch_cancelled(self, accounts: list[dict[str, Any]]) -> None:
        targets = {str(item.get("id") or "").strip() for item in accounts}

        def update(raw: Any) -> list[dict[str, Any]]:
            items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in items:
                if str(item.get("id") or "").strip() not in targets:
                    continue
                if str(item.get("health_status") or "").lower() not in {"checking", "recovering"}:
                    continue
                item.update({
                    "health_status": "unchecked",
                    "health_alive": False,
                    "health_detail": "本轮检测/恢复已停止",
                    "health_recovery_status": "stopped",
                    "health_recovery_updated_at": _now(),
                })
            return items

        self.store.update("accounts.json", [], update)

    def _run_batch(self, settings: dict[str, Any]) -> None:
        health = settings.get("health") if isinstance(settings.get("health"), dict) else {}
        check_concurrency = max(1, min(50, int(health.get("concurrency") or 3)))
        recovery_concurrency = max(1, min(50, int(health.get("recovery_concurrency") or 3)))
        with self._lock:
            self._state["check_concurrency"] = check_concurrency
            self._state["recovery_concurrency"] = recovery_concurrency
        probe_futures: dict[Any, dict[str, Any]] = {}
        recovery_futures: dict[Any, dict[str, Any]] = {}
        try:
            with (
                ThreadPoolExecutor(max_workers=check_concurrency, thread_name_prefix="account-probe") as probe_executor,
                ThreadPoolExecutor(max_workers=recovery_concurrency, thread_name_prefix="account-recovery") as recovery_executor,
            ):
                while True:
                    if self._batch_cancel.is_set():
                        break
                    while not self._batch_cancel.is_set():
                        try:
                            account = self._batch_queue.get_nowait()
                        except Empty:
                            break
                        future = probe_executor.submit(self._probe_one, settings, account)
                        probe_futures[future] = account

                    active_futures = set(probe_futures) | set(recovery_futures)
                    if not active_futures:
                        with self._lock:
                            if self._batch_queue.empty():
                                self._batch_accepting = False
                                break
                        continue

                    completed, _pending = wait(active_futures, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        if self._batch_cancel.is_set():
                            break
                        if future.cancelled():
                            probe_futures.pop(future, None)
                            recovery_futures.pop(future, None)
                            continue
                        if future in probe_futures:
                            account = probe_futures.pop(future)
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = self._failed_result(str(exc))
                            with self._lock:
                                self._state["probed"] += 1
                            if result.get("needs_recovery"):
                                try:
                                    self._mark_recovering(account)
                                except Exception as exc:
                                    self.manager.log("warning", f"恢复队列状态写入失败，继续恢复：{str(exc)[:240]}")
                                with self._lock:
                                    self._state["recovery_total"] += 1
                                recovery_future = recovery_executor.submit(self._recover_one, settings, account, result)
                                recovery_futures[recovery_future] = account
                            else:
                                self._record_result_safely(account, result)
                        else:
                            account = recovery_futures.pop(future)
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = self._failed_result(str(exc))
                            with self._lock:
                                self._state["recovery_completed"] += 1
                            self._record_result_safely(account, result)
        finally:
            for pending in probe_futures:
                pending.cancel()
            for pending in recovery_futures:
                pending.cancel()
            cancelled = self._batch_cancel.is_set()
            with self._lock:
                self._batch_accepting = False
                self._recovery_progress = {}
                accounts = list(self._batch_accounts)
            if cancelled:
                try:
                    self._mark_batch_cancelled(accounts)
                except Exception as exc:
                    self.manager.log("warning", f"停止状态写入失败：{str(exc)[:300]}")
            with self._lock:
                self._state["state"] = "cancelled" if cancelled else "completed"
                self._state["finished_at"] = _now()
                self._state["current_email"] = ""
                self._state["message"] = "本轮账号检测/恢复已停止" if cancelled else "账号检测完成"
            status = self.status()
            if cancelled:
                self.manager.log(
                    "warning",
                    f"账号检测/恢复已停止：已完成 {status['checked']} / {status['total']} 个",
                )
            else:
                self.manager.log(
                    "info",
                    "账号检测完成："
                    f"存活 {status['alive']}，恢复 {status['recovered']}，"
                    f"封禁 {status['banned']}，失效 {status['expired']}，异常 {status['failed']}",
                )

    def _monitor_loop(self) -> None:
        while not self._shutdown.is_set():
            settings = self.store.settings()
            health = settings.get("health") if isinstance(settings.get("health"), dict) else {}
            enabled = bool(health.get("auto_check_enabled", False))
            interval = max(self.minimum_interval, float(health.get("interval_seconds") or 300))
            with self._lock:
                self._state["auto_enabled"] = enabled
            if enabled and self.manager.status().get("state") not in {"running", "stopping"}:
                try:
                    self.start_check(source="auto")
                except RuntimeError:
                    pass
            self._wake.wait(timeout=interval)
            self._wake.clear()
