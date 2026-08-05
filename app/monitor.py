from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.cloud import CloudClient, capacity_estimate, capacity_status_label
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
            "active_job_target": 0,
            "active_job_started": 0,
            "active_job_running": 0,
            "active_job_pending": 0,
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
        active_monitor_job = job.get("state") == "running" and str(job.get("source") or "manual") == "monitor"
        if job.get("state") in {"running", "stopping"} and not active_monitor_job:
            with self._lock:
                self._state["state"] = "job_running"
                self._state["message"] = "注册任务运行中"
                self._state["last_job_id"] = str(job.get("job_id") or "")
                self._state["active_job_target"] = int(job.get("target_total") or job.get("total") or 0)
                self._state["active_job_started"] = int(job.get("started") or 0)
                self._state["active_job_running"] = int(job.get("running") or 0)
                self._state["active_job_pending"] = int(job.get("pending") or 0)
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
                f"状态：{capacity_status_label(status)}，建议注册：{need}，"
                f"当前可调度账号：{estimate['current_effective_accounts']}，"
                f"缺可用账号：{estimate['recommended_add_usable_accounts']}，"
                f"可调度槽位：{estimate['dispatchable_slots']}，"
                f"空闲槽位：{estimate['idle_slots']}，租用槽位：{estimate['leased_slots']}，"
                f"冷却中：{estimate['cooling']}，受限账号：{estimate['limited']}，"
                f"无效账号：{estimate['invalid']}，死号：{estimate['dead']}",
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

        # A monitor-owned job keeps the same absolute target while it runs.
        # Refreshing capacity on every poll lets us lower that target without
        # cancelling workers already inside an email/code/token workflow, or
        # raise it when the cloud reports a larger shortage.
        if active_monitor_job:
            batch_limit = max(1, min(100, int(cloud.get("monitor_batch_limit") or 20)))
            desired = min(need, batch_limit) if status == "shortage" and need > 0 else 0
            adjust = getattr(self.manager, "adjust_target", None)
            adjustment: dict[str, Any] = {}
            if callable(adjust):
                adjustment = adjust(desired, job_id=str(job.get("job_id") or "")) or {}
            previous = int(adjustment.get("previous_target") or job.get("target_total") or job.get("total") or 0)
            target = int(adjustment.get("target_total") if adjustment.get("target_total") is not None else desired)
            with self._lock:
                self._state["last_job_id"] = str(job.get("job_id") or "")
                self._state["active_job_target"] = target
                self._state["active_job_started"] = int(adjustment.get("started") or job.get("started") or 0)
                self._state["active_job_running"] = int(job.get("running") or 0)
                self._state["active_job_pending"] = int(adjustment.get("pending") if adjustment.get("pending") is not None else job.get("pending") or 0)
                self._state["state"] = "registering"
                if target != previous:
                    if target < previous:
                        self._state["message"] = (
                            f"需求下降，注册目标已从 {previous} 调整为 {target}；"
                            "已开始的账号继续完成，未开始的任务停止领取"
                        )
                    else:
                        self._state["message"] = f"需求增加，注册目标已从 {previous} 调整为 {target}"
            if target != previous:
                if target < previous:
                    self.manager.log(
                        "warning",
                        f"自动监听重新调整注册目标：原目标 {previous}，现目标 {target}；"
                        "已开始任务继续完成，未开始任务停止领取",
                    )
                else:
                    self.manager.log("info", f"自动监听增加注册目标：原目标 {previous}，现目标 {target}")
            self._observations = 0
            return
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
        # ``source`` is understood by the real manager. The fallback keeps
        # older test doubles and integrations source-compatible while they are
        # upgraded.
        try:
            result = self.manager.start(
                count=count,
                concurrency=concurrency,
                provider="openai",
                channel=channel,
                source="monitor",
            )
        except TypeError as exc:
            if "source" not in str(exc):
                raise
            result = self.manager.start(count=count, concurrency=concurrency, provider="openai", channel=channel)
        self._observations = 0
        with self._lock:
            self._state["state"] = "registering"
            self._state["message"] = f"已自动启动 {count} 个注册任务"
            self._state["shortage_observations"] = 0
            self._state["last_job_id"] = str(result.get("job_id") or "")
            self._state["active_job_target"] = count
            self._state["active_job_started"] = int(result.get("started") or 0)
            self._state["active_job_running"] = int(result.get("running") or 0)
            self._state["active_job_pending"] = int(result.get("pending") or count)
