from __future__ import annotations

import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.cloud import CloudClient
from app.registration import BrowserRegistrar, ProtocolRegistrar
from app.storage import JsonStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(value: Any, head: int = 10, tail: int = 6) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= head + tail + 3:
        return "*" * min(len(text), 12)
    return f"{text[:head]}...{text[-tail:]}"


def _seconds_between(started_at: Any, ended_at: Any) -> int:
    try:
        start = datetime.fromisoformat(str(started_at or "").replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(ended_at or "").replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except (TypeError, ValueError):
        return 0


class RegistrationManager:
    """Coordinates the ChatGPT registration workflow."""

    _PROVIDER = "openai"

    def __init__(
        self,
        store: JsonStore,
        registrar_factory: Callable[..., ProtocolRegistrar] = ProtocolRegistrar,
        browser_registrar_factory: Callable[..., BrowserRegistrar] = BrowserRegistrar,
    ) -> None:
        self.store = store
        self.registrar_factory = registrar_factory
        self.browser_registrar_factory = browser_registrar_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._runner: threading.Thread | None = None
        self._log_context = threading.local()
        self._log_sequence = 0
        self._logs: list[dict[str, Any]] = []
        self._state = self._new_state()
        self.store.read("accounts.json", [])

    @classmethod
    def _validate_provider(cls, provider: str | None) -> str:
        normalized = str(provider or cls._PROVIDER).strip().lower()
        if normalized != cls._PROVIDER:
            raise ValueError("仅支持 ChatGPT 注册")
        return normalized

    @classmethod
    def _new_state(cls) -> dict[str, Any]:
        return {
            "job_id": "",
            "provider": cls._PROVIDER,
            "channel": "protocol",
            "state": "idle",
            "total": 0,
            "started": 0,
            "running": 0,
            "success": 0,
            "failed": 0,
            "started_at": "",
            "finished_at": "",
            "errors": [],
        }

    @staticmethod
    def _with_elapsed(state: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(state)
        if not result.get("started_at"):
            result["elapsed_seconds"] = 0
            return result
        try:
            start = datetime.fromisoformat(str(result["started_at"]))
            end = datetime.fromisoformat(str(result["finished_at"])) if result.get("finished_at") else datetime.now(timezone.utc)
            result["elapsed_seconds"] = max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError):
            result["elapsed_seconds"] = 0
        return result

    def log(self, level: str, message: str, task_number: int | None = None, provider: str | None = None) -> None:
        if provider is not None:
            self._validate_provider(provider)
        with self._lock:
            self._log_sequence += 1
            item: dict[str, Any] = {
                "cursor": self._log_sequence,
                "time": _now(),
                "level": level if level in {"info", "success", "warning", "error"} else "info",
                "message": str(message)[:1000],
                "provider": self._PROVIDER,
            }
            if task_number is not None:
                item["task"] = task_number
            self._logs.append(item)
            if len(self._logs) > 1500:
                self._logs = self._logs[-1000:]

    def status(self, provider: str | None = None) -> dict[str, Any]:
        self._validate_provider(provider)
        with self._lock:
            state = self._with_elapsed(self._state)
        state["providers"] = {self._PROVIDER: copy.deepcopy(state)}
        state["active_providers"] = [
            self._PROVIDER if state.get("state") in {"running", "stopping"} else ""
        ]
        state["active_providers"] = [value for value in state["active_providers"] if value]
        return state

    def logs(self, cursor: int = 0) -> dict[str, Any]:
        with self._lock:
            items = [copy.deepcopy(item) for item in self._logs if int(item["cursor"]) > int(cursor)]
            next_cursor = self._log_sequence
        return {"items": items, "cursor": next_cursor}

    def accounts(self, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        raw = self.store.read("accounts.json", [])
        if not isinstance(raw, list):
            return []
        now = _now()
        items: list[dict[str, Any]] = []
        for value in raw:
            if not isinstance(value, dict):
                continue
            if str(value.get("provider") or self._PROVIDER).strip().lower() != self._PROVIDER:
                continue
            item = copy.deepcopy(value)
            for field in (
                "chatgpt_access_token", "chatgpt_session_error", "codex_auth",
                "codex_agent_identity", "codex_agent_status", "codex_agent_created_at",
                "codex_agent_error", "sub2api_sync_status", "sub2api_synced_at",
                "sub2api_sync_error", "sub2api_expires_at", "sub2api_expiry_minutes",
                "sub2api_remote_account_ids", "sub2api_import_result",
            ):
                item.pop(field, None)
            item["provider"] = self._PROVIDER
            item["has_password"] = bool(str(value.get("password") or "").strip())
            item["has_access_token"] = bool(str(value.get("access_token") or "").strip())
            item["has_refresh_token"] = bool(str(value.get("refresh_token") or "").strip())
            item["has_id_token"] = bool(str(value.get("id_token") or "").strip())
            item["survival_started_at"] = str(value.get("survival_started_at") or value.get("created_at") or now)
            item["survival_ended_at"] = str(value.get("survival_ended_at") or "")
            item["survival_seconds"] = _seconds_between(item["survival_started_at"], item["survival_ended_at"] or now)
            completed_seconds = max(0, int(value.get("survival_total_seconds") or 0))
            current_seconds = 0 if bool(value.get("survival_interval_recorded")) else item["survival_seconds"]
            item["survival_total_with_current_seconds"] = completed_seconds + current_seconds
            if not include_secrets:
                item["password"] = "********"
                item["access_token"] = _mask(item.get("access_token"))
                item["refresh_token"] = _mask(item.get("refresh_token"))
                item["id_token"] = _mask(item.get("id_token"))
            items.append(item)
        return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    @staticmethod
    def _account_category(item: dict[str, Any], category: str) -> bool:
        status = str(item.get("health_status") or "unchecked").lower()
        recovery = str(item.get("health_recovery_status") or "").lower()
        awaiting_recovery = status in {"expired", "recovering"} or recovery in {
            "queued", "running", "failed", "missing_credentials", "token_refreshed", "stopped", "interrupted",
        }
        if category == "alive":
            return status == "alive"
        if category == "recovery":
            return awaiting_recovery
        if category == "recovered":
            return status == "alive" and recovery == "recovered"
        if category == "attention":
            return status != "alive" and not awaiting_recovery
        return True

    def accounts_page(self, *, page: int = 1, page_size: int = 20, query: str = "", category: str = "all") -> dict[str, Any]:
        safe_page_size = max(5, min(100, int(page_size or 20)))
        items = self.accounts()
        all_total = len(items)
        search = str(query or "").strip().lower()[:100]
        if search:
            fields = ("email", "id", "health_status", "health_recovery_status", "health_detail")
            items = [item for item in items if any(search in str(item.get(field) or "").lower() for field in fields)]
        counts = {name: sum(1 for item in items if self._account_category(item, name)) for name in ("all", "alive", "recovery", "recovered", "attention")}
        resolved_category = category if category in counts else "all"
        items = [item for item in items if self._account_category(item, resolved_category)]
        total = len(items)
        pages = max(1, (total + safe_page_size - 1) // safe_page_size)
        safe_page = max(1, min(int(page or 1), pages))
        start = (safe_page - 1) * safe_page_size
        return {
            "items": items[start : start + safe_page_size], "total": total, "page": safe_page,
            "page_size": safe_page_size, "pages": pages, "all_total": all_total,
            "query": search, "category": resolved_category, "counts": counts,
        }

    def account_credentials(self, account_id: str) -> dict[str, Any]:
        target = str(account_id or "").strip()
        for item in self.accounts(include_secrets=True):
            if str(item.get("id") or "").strip() != target:
                continue
            refresh_token = str(item.get("refresh_token") or "").strip()
            return {
                "id": target,
                "email": str(item.get("email") or ""),
                "password": str(item.get("password") or ""),
                "has_refresh_token": bool(refresh_token),
                "refresh_token_masked": _mask(refresh_token),
            }
        raise KeyError(target)

    def account_summary(self) -> dict[str, int]:
        accounts = self.accounts()
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "total": len(accounts),
            "today": sum(1 for item in accounts if str(item.get("created_at") or "").startswith(today)),
        }

    def _record_registration_result(self, success: bool) -> None:
        """Persist registration outcomes so periodic reports survive restarts."""
        day = datetime.now(timezone.utc).date().isoformat()
        key = "success" if success else "failed"

        def update(raw: Any) -> dict[str, Any]:
            payload = raw if isinstance(raw, dict) else {}
            payload["total_success"] = int(payload.get("total_success") or 0)
            payload["total_failed"] = int(payload.get("total_failed") or 0)
            payload["days"] = payload.get("days") if isinstance(payload.get("days"), dict) else {}
            entry = payload["days"].setdefault(day, {"date": day, "success": 0, "failed": 0})
            entry["date"] = day
            entry["success"] = int(entry.get("success") or 0)
            entry["failed"] = int(entry.get("failed") or 0)
            entry[key] += 1
            total_key = "total_success" if success else "total_failed"
            payload[total_key] += 1
            payload["last_at"] = _now()
            return payload

        try:
            self.store.update("registration_stats.json", {"total_success": 0, "total_failed": 0, "days": {}}, update)
        except Exception as exc:
            self.log("warning", f"注册统计写入失败：{str(exc)[:300]}")

    def registration_report(self) -> dict[str, Any]:
        """Return aggregate and today's registration success metrics."""
        payload = self.store.read("registration_stats.json", {"total_success": 0, "total_failed": 0, "days": {}})
        if not isinstance(payload, dict):
            payload = {}
        total_success = max(0, int(payload.get("total_success") or 0))
        total_failed = max(0, int(payload.get("total_failed") or 0))
        today_key = datetime.now(timezone.utc).date().isoformat()
        days = payload.get("days") if isinstance(payload.get("days"), dict) else {}
        today = days.get(today_key) if isinstance(days.get(today_key), dict) else {}
        today_success = max(0, int(today.get("success") or 0))
        today_failed = max(0, int(today.get("failed") or 0))
        if total_success + total_failed == 0:
            # Existing installations predate registration_stats.json. Use the
            # persisted account list as a useful success-only baseline until
            # the first failed or successful attempt is recorded.
            account_totals = self.account_summary()
            total_success = max(total_success, int(account_totals.get("total") or 0))
            today_success = max(today_success, int(account_totals.get("today") or 0))

        def rate(success: int, failed: int) -> float:
            attempts = success + failed
            return round(success * 100 / attempts, 2) if attempts else 0.0

        return {
            "today": {
                "success": today_success,
                "failed": today_failed,
                "attempts": today_success + today_failed,
                "success_rate": rate(today_success, today_failed),
            },
            "total": {
                "success": total_success,
                "failed": total_failed,
                "attempts": total_success + total_failed,
                "success_rate": rate(total_success, total_failed),
            },
            "last_at": str(payload.get("last_at") or ""),
        }

    def _persist_account(self, account: dict[str, Any]) -> None:
        payload = copy.deepcopy(account)
        payload["provider"] = self._PROVIDER
        timestamp = str(payload.get("created_at") or _now())
        payload.setdefault("survival_started_at", timestamp)
        payload.setdefault("survival_ended_at", "")
        payload.setdefault("survival_last_seconds", 0)
        payload.setdefault("survival_total_seconds", 0)
        payload.setdefault("survival_recovery_count", 0)
        payload.setdefault("survival_interval_recorded", False)

        def update(value: Any) -> list[dict[str, Any]]:
            items = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
            email = str(payload.get("email") or "").strip().lower()
            items = [
                item for item in items
                if not (
                    str(item.get("provider") or self._PROVIDER).strip().lower() == self._PROVIDER
                    and str(item.get("email") or "").strip().lower() == email
                )
            ]
            items.append(copy.deepcopy(payload))
            return items

        self.store.update("accounts.json", [], update)

    def _update_account(self, account: dict[str, Any], values: dict[str, Any]) -> None:
        account_id = str(account.get("id") or "").strip()
        email = str(account.get("email") or "").strip().lower()

        def update(items: Any) -> list[dict[str, Any]]:
            accounts = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
            for item in accounts:
                if str(item.get("provider") or self._PROVIDER).strip().lower() != self._PROVIDER:
                    continue
                same_id = account_id and str(item.get("id") or "").strip() == account_id
                same_email = email and str(item.get("email") or "").strip().lower() == email
                if same_id or same_email:
                    item.update(copy.deepcopy(values))
                    break
            return accounts

        self.store.update("accounts.json", [], update)

    def mark_skipped(
        self,
        message: str,
        *,
        provider: str = "openai",
        channel: str | None = None,
    ) -> dict[str, Any]:
        self._validate_provider(provider)
        resolved_channel = self._validate_channel(channel)
        timestamp = _now()
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise RuntimeError("已有注册任务正在运行")
            self._state = {
                **self._new_state(), "job_id": uuid.uuid4().hex, "state": "skipped",
                "channel": resolved_channel,
                "started_at": timestamp, "finished_at": timestamp, "message": str(message or "云端容量充足"),
            }
        self.log("info", str(message or "云端容量充足，本轮跳过注册"))
        return self.status()

    def delete_accounts(self, account_ids: list[str]) -> int:
        targets = {str(value).strip() for value in account_ids if str(value).strip()}
        if not targets:
            return 0
        removed = 0

        def update(value: Any) -> list[dict[str, Any]]:
            nonlocal removed
            items = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
            kept = [
                item for item in items
                if str(item.get("provider") or self._PROVIDER).strip().lower() != self._PROVIDER
                or str(item.get("id") or "") not in targets
            ]
            removed = len(items) - len(kept)
            return kept

        self.store.update("accounts.json", [], update)
        return removed

    @staticmethod
    def _validate_channel(channel: str | None) -> str:
        normalized = str(channel or "protocol").strip().lower()
        if normalized not in {"protocol", "browser"}:
            raise ValueError("注册渠道仅支持 protocol 或 browser")
        return normalized

    def start(
        self,
        *,
        count: int,
        concurrency: int,
        provider: str = "openai",
        channel: str | None = None,
    ) -> dict[str, Any]:
        self._validate_provider(provider)
        count = max(1, min(100, int(count)))
        concurrency = max(1, min(50, int(concurrency), count))
        with self._lock:
            if self._runner and self._runner.is_alive():
                raise RuntimeError("已有注册任务正在运行")
            self._stop_event = threading.Event()
            job_id = uuid.uuid4().hex
            settings = copy.deepcopy(self.store.settings())
            registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
            resolved_channel = self._validate_channel(channel or registration.get("channel"))
            registration["channel"] = resolved_channel
            settings["registration"] = registration
            self._state = {
                **self._new_state(), "job_id": job_id, "state": "running", "channel": resolved_channel,
                "total": count, "started_at": _now(),
            }
            settings["_data_root"] = str(self.store.root)
            settings["outlook_pool_path"] = str(self.store.path("outlook_mailboxes.json"))
            self._runner = threading.Thread(
                target=self._run, args=(settings, count, concurrency, self._stop_event, job_id),
                name="registration-manager-openai", daemon=True,
            )
            self._runner.start()
        channel_label = "浏览器模拟" if resolved_channel == "browser" else "协议"
        self.log("info", f"ChatGPT {channel_label}注册批次启动：目标 {count} 个，并发 {concurrency}")
        return self.status()

    def stop(self, provider: str | None = None) -> dict[str, Any]:
        self._validate_provider(provider)
        with self._lock:
            if self._state.get("state") in {"running", "stopping"}:
                self._state["state"] = "stopping"
                self._stop_event.set()
        self.log("warning", "ChatGPT 收到停止请求，正在结束当前网络步骤")
        return self.status()

    def _run(self, settings: dict[str, Any], count: int, concurrency: int, stop_event: threading.Event, job_id: str) -> None:
        queue_lock = threading.Lock()
        next_task = 1
        registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        channel = self._validate_channel(registration.get("channel"))
        registrar_factory = self.browser_registrar_factory if channel == "browser" else self.registrar_factory

        def worker() -> None:
            nonlocal next_task
            while not stop_event.is_set():
                with queue_lock:
                    if next_task > count:
                        return
                    task_number = next_task
                    next_task += 1
                with self._lock:
                    if self._state.get("job_id") != job_id:
                        return
                    self._state["started"] += 1
                    self._state["running"] += 1
                self.log("info", "开始注册", task_number)
                registrar = None
                try:
                    registrar = registrar_factory(
                        copy.deepcopy(settings),
                        logger=lambda level, message, number=task_number: self.log(level, message, number),
                        stop_event=stop_event,
                    )
                    account = registrar.register()
                    account["provider"] = self._PROVIDER
                    account.setdefault("registration_channel", channel)
                    cloud_settings = settings.get("cloud") if isinstance(settings.get("cloud"), dict) else {}
                    should_upload = bool(cloud_settings.get("enabled")) and bool(cloud_settings.get("upload_accounts", True))
                    if should_upload:
                        account["cloud_sync_status"] = "pending"
                    self._persist_account(account)
                    with self._lock:
                        if self._state.get("job_id") == job_id:
                            self._state["success"] += 1
                    self._record_registration_result(True)
                    self.log("success", f"账号已写入 JSON：{account.get('email') or 'unknown'}", task_number)

                    if should_upload:
                        self._upload_cloud(account, cloud_settings, settings, task_number)
                except Exception as exc:
                    message = str(exc)[:500]
                    with self._lock:
                        if self._state.get("job_id") == job_id:
                            self._state["failed"] += 1
                            self._state["errors"] = (self._state["errors"] + [{"task": task_number, "message": message}])[-20:]
                    self._record_registration_result(False)
                    self.log("warning" if stop_event.is_set() else "error", f"注册失败：{message}", task_number)
                finally:
                    if registrar is not None:
                        try:
                            registrar.close()
                        except Exception:
                            pass
                    with self._lock:
                        if self._state.get("job_id") == job_id:
                            self._state["running"] = max(0, self._state["running"] - 1)

        try:
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="register-openai-worker") as executor:
                futures = [executor.submit(worker) for _ in range(concurrency)]
                for future in futures:
                    future.result()
        finally:
            with self._lock:
                if self._state.get("job_id") == job_id:
                    self._state["state"] = "stopped" if stop_event.is_set() else "completed"
                    self._state["finished_at"] = _now()
            status = self.status()
            self.log("success" if status["failed"] == 0 else "warning", f"注册批次结束：成功 {status['success']}，失败 {status['failed']}")

    def _upload_cloud(self, account: dict[str, Any], settings: dict[str, Any], runtime: dict[str, Any], task_number: int) -> None:
        self.log("info", "开始上传云端", task_number)
        try:
            cloud_account = copy.deepcopy(account)
            result = CloudClient(settings, str(runtime.get("registration", {}).get("proxy") or "")).upload_account(cloud_account)
            values = {"cloud_sync_status": "synced", "cloud_synced_at": _now(), "cloud_sync_error": "", "cloud_import_result": {key: result.get(key) for key in ("added", "skipped", "refreshed")}}
            self._update_account(account, values)
            self.log("success", f"云端上传完成：added={int(result.get('added') or 0)}，skipped={int(result.get('skipped') or 0)}，refreshed={int(result.get('refreshed') or 0)}", task_number)
        except Exception as exc:
            values = {"cloud_sync_status": "failed", "cloud_synced_at": _now(), "cloud_sync_error": str(exc)[:500]}
            self._update_account(account, values)
            self.log("error", f"云端上传失败：{values['cloud_sync_error']}", task_number)

    def shutdown(self) -> None:
        with self._lock:
            if self._state.get("state") in {"running", "stopping"}:
                self._state["state"] = "stopping"
                self._stop_event.set()
            runner = self._runner
        if runner and runner.is_alive():
            runner.join(timeout=3)
