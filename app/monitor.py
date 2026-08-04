from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.cloud import CloudClient, capacity_estimate
from app.manager import RegistrationManager
from app.storage import DEFAULT_SETTINGS, JsonStore, deep_merge


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudRegistrationMonitor:
    def __init__(
        self,
        store: JsonStore,
        manager: RegistrationManager,
        cloud_factory: Callable[..., CloudClient] = CloudClient,
        minimum_interval: float = 5.0,
    ) -> None:
        self.store = store
        self.manager = manager
        self.cloud_factory = cloud_factory
        self.minimum_interval = max(0.01, float(minimum_interval))
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._observations = 0
        self._last_signature = ""
        self._last_logged_error = ""
        self._state: dict[str, Any] = {
            "enabled": False,
            "state": "disabled",
            "last_checked_at": "",
            "capacity_status": "",
            "recommended_register_accounts": 0,
            "current_effective_accounts": 0,
            "dispatchable_slots": 0,
            "idle_slots": 0,
            "leased_slots": 0,
            "cooling": 0,
            "limited": 0,
            "invalid": 0,
            "dead": 0,
            "shortage_observations": 0,
            "shortage_confirmations": 0,
            "last_job_id": "",
            "last_error": "",
            "message": "自动监听未开启",
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._thread = threading.Thread(target=self._run, name="cloud-registration-monitor", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = copy.deepcopy(self._state)
            result["thread_alive"] = bool(self._thread and self._thread.is_alive())
        return result

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        def update(raw: Any) -> dict[str, Any]:
            settings = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
            cloud = settings.setdefault("cloud", {})
            cloud["monitor_enabled"] = bool(enabled)
            return settings

        self.store.update("settings.json", DEFAULT_SETTINGS, update)
        with self._lock:
            self._state["enabled"] = bool(enabled)
            self._state["state"] = "waiting" if enabled else "disabled"
            self._state["message"] = "等待容量检查" if enabled else "自动监听已停止"
            if not enabled:
                self._observations = 0
                self._state["shortage_observations"] = 0
        self.wake()
        return self.status()

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.01, seconds))
        self._wake.clear()

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                settings = self.store.settings()
                cloud = settings.get("cloud") if isinstance(settings.get("cloud"), dict) else {}
                enabled = bool(cloud.get("monitor_enabled", False))
                interval = max(
                    self.minimum_interval,
                    float(cloud.get("monitor_interval_seconds") or 30),
                )
                with self._lock:
                    self._state["enabled"] = enabled
                if not enabled:
                    with self._lock:
                        self._state["state"] = "disabled"
                        self._state["message"] = "自动监听未开启"
                    self._wait(interval)
                    continue
                self._check(settings, cloud)
                self._wait(interval)
            except Exception as exc:
                detail = str(exc)[:500]
                with self._lock:
                    self._state["state"] = "error"
                    self._state["last_error"] = detail
                    self._state["message"] = detail
                if detail != self._last_logged_error:
                    self.manager.log("error", f"自动监听异常：{detail}")
                    self._last_logged_error = detail
                self._wait(self.minimum_interval)

    def _check(self, settings: dict[str, Any], cloud: dict[str, Any]) -> None:
        job = self.manager.status(provider="openai")
        if job.get("state") in {"running", "stopping"}:
            with self._lock:
                self._state["state"] = "job_running"
                self._state["message"] = "注册任务运行中"
                self._state["last_job_id"] = str(job.get("job_id") or "")
            return
        if not bool(cloud.get("enabled")):
            raise RuntimeError("自动监听已开启，但云端配置未启用")
        if not bool(cloud.get("use_capacity", True)):
            raise RuntimeError("自动监听需要开启云端容量模型")

        with self._lock:
            self._state["state"] = "checking"
            self._state["message"] = "正在读取云端容量"
        registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        payload = self.cloud_factory(cloud, str(registration.get("proxy") or "")).capacity()
        estimate = capacity_estimate(payload)
        status = estimate["status"]
        need = int(estimate["recommended_register_accounts"] or 0)
        confirmations = max(1, min(10, int(cloud.get("shortage_confirmations") or 2)))
        signature = (
            f"{status}:{need}:{estimate['current_effective_accounts']}:{estimate['dispatchable_slots']}"
            f":{estimate['idle_slots']}:{estimate['leased_slots']}:{estimate['cooling']}"
            f":{estimate['limited']}:{estimate['invalid']}:{estimate['dead']}"
        )
        if signature != self._last_signature:
            self.manager.log(
                "info",
                "自动监听容量："
                f"status={status}，建议注册={need}，"
                f"当前可调度={estimate['current_effective_accounts']}，"
                f"缺可用={estimate['recommended_add_usable_accounts']}，"
                f"dispatchable_slots={estimate['dispatchable_slots']}，"
                f"idle_slots={estimate['idle_slots']}，leased_slots={estimate['leased_slots']}，"
                f"cooling={estimate['cooling']}，limited={estimate['limited']}，"
                f"invalid={estimate['invalid']}，dead={estimate['dead']}",
            )
            self._last_signature = signature
        self._last_logged_error = ""

        if status == "shortage" and need > 0:
            self._observations += 1
        else:
            self._observations = 0
        with self._lock:
            self._state.update(
                {
                    "last_checked_at": _now(),
                    "capacity_status": status,
                    "recommended_register_accounts": need,
                    "current_effective_accounts": estimate["current_effective_accounts"],
                    "dispatchable_slots": estimate["dispatchable_slots"],
                    "idle_slots": estimate["idle_slots"],
                    "leased_slots": estimate["leased_slots"],
                    "cooling": estimate["cooling"],
                    "limited": estimate["limited"],
                    "invalid": estimate["invalid"],
                    "dead": estimate["dead"],
                    "shortage_observations": self._observations,
                    "shortage_confirmations": confirmations,
                    "last_error": "",
                    "state": "shortage" if status == "shortage" and need > 0 else "waiting",
                    "message": estimate["message"] or "等待下一次容量检查",
                }
            )
        if status != "shortage" or need <= 0:
            return
        if self._observations < confirmations:
            self.manager.log("warning", f"自动监听缺口确认：{self._observations}/{confirmations}")
            return

        batch_limit = max(1, min(100, int(cloud.get("monitor_batch_limit") or 20)))
        count = min(need, batch_limit)
        concurrency = max(1, min(50, int(cloud.get("monitor_concurrency") or 5), count))
        channel = str(registration.get("channel") or "protocol").strip().lower()
        if channel not in {"protocol", "browser"}:
            channel = "protocol"
        self.manager.log("warning", f"云端缺口连续确认，自动启动注册：数量 {count}，并发 {concurrency}")
        result = self.manager.start(count=count, concurrency=concurrency, provider="openai", channel=channel)
        self._observations = 0
        with self._lock:
            self._state["state"] = "registering"
            self._state["message"] = f"已自动启动 {count} 个注册任务"
            self._state["shortage_observations"] = 0
            self._state["last_job_id"] = str(result.get("job_id") or "")
