from __future__ import annotations

import copy
import json
import threading
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from typing import Any

from app.cloud import CloudClient, capacity_estimate
from app.registration.outlook import OutlookMailboxPool
from app.storage import JsonStore
from app.time_utils import iso_now, today as china_today


def _now() -> str:
    return iso_now()


class BarkStockNotifier:
    """Notify once when the Outlook registration pool drops below a threshold."""

    def __init__(self, store: JsonStore, manager: Any, minimum_interval: float = 5.0) -> None:
        self.store = store
        self.manager = manager
        self.minimum_interval = max(1.0, float(minimum_interval))
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._notified_low = False
        self._last_report_monotonic = 0.0
        self._last_error = ""
        self._state: dict[str, Any] = {
            "enabled": False,
            "state": "disabled",
            "available_slots": 0,
            "threshold": 100,
            "last_checked_at": "",
            "last_notified_at": "",
            "report_enabled": False,
            "report_interval_seconds": 3600,
            "last_report_at": "",
            "last_report_error": "",
            "last_error": "",
            "message": "Bark 低库存通知未开启",
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._thread = threading.Thread(target=self._run, name="bark-stock-notifier", daemon=True)
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

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.01, seconds))
        self._wake.clear()

    @staticmethod
    def _bark_url(base: str, key: str, title: str, body: str) -> str:
        endpoint = str(base or "https://api.day.app").strip().rstrip("/")
        if "{key}" in endpoint:
            endpoint = endpoint.replace("{key}", quote(key, safe=""))
        elif key:
            endpoint = f"{endpoint}/{quote(key, safe='')}"
        return f"{endpoint}/{quote(title, safe='')}/{quote(body, safe='')}"

    def _send(self, url: str, key: str, title: str, body: str) -> None:
        target = self._bark_url(url, key, title, body)
        request = Request(target, headers={"User-Agent": "GPT-REG-TOOLS/1.0", "Accept": "application/json"})
        with urlopen(request, timeout=10) as response:
            if int(getattr(response, "status", 200) or 200) >= 400:
                raise RuntimeError(f"Bark HTTP {response.status}")
            response.read(256)

    def _mail_import_summary(self) -> dict[str, int]:
        stats = self.store.read("outlook_import_stats.json", {"days": {}})
        days = stats.get("days") if isinstance(stats, dict) and isinstance(stats.get("days"), dict) else {}
        today_key = china_today()
        today = days.get(today_key) if isinstance(days.get(today_key), dict) else {}
        total_added = 0
        for entry in days.values():
            if isinstance(entry, dict):
                total_added += max(0, int(entry.get("added") or 0))
        return {
            "today_added": max(0, int(today.get("added") or 0)),
            "total_added": total_added,
        }

    def _report_body(
        self,
        provider: str,
        summary: dict[str, Any],
        registration_report: dict[str, Any],
        cloud_estimate: dict[str, Any] | None = None,
    ) -> str:
        today = registration_report.get("today") if isinstance(registration_report.get("today"), dict) else {}
        total = registration_report.get("total") if isinstance(registration_report.get("total"), dict) else {}
        imports = self._mail_import_summary()
        provider_label = "Outlook" if provider == "outlook" else provider.upper()
        recorded_total = imports["total_added"]
        current_pool_total = max(0, int(summary.get("total") or 0))
        if recorded_total == 0:
            recorded_total = current_pool_total
        lines = [
            f"邮箱来源：{provider_label}\n"
            f"今日注册成功率：{float(today.get('success_rate') or 0):g}% "
            f"（成功 {int(today.get('success') or 0)} / 尝试 {int(today.get('attempts') or 0)}）\n"
            f"累计注册成功率：{float(total.get('success_rate') or 0):g}% "
            f"（成功 {int(total.get('success') or 0)} / 尝试 {int(total.get('attempts') or 0)}）\n"
            f"邮箱导入：今日 {imports['today_added']} 个，累计 {recorded_total} 个（当前池 {current_pool_total} 个）\n"
            f"剩余可注册数：{int(summary.get('available_slots') or 0)} 个"
        ]
        if cloud_estimate:
            lines.append(
                "云端账号："
                f"可调度槽位 {int(cloud_estimate.get('dispatchable_slots') or 0)}，"
                f"空闲 {int(cloud_estimate.get('idle_slots') or 0)}，"
                f"租用 {int(cloud_estimate.get('leased_slots') or 0)}，"
                f"冷却 {int(cloud_estimate.get('cooling') or 0)}，"
                f"受限 {int(cloud_estimate.get('limited') or 0)}，"
                f"无效 {int(cloud_estimate.get('invalid') or 0)}，"
                f"死号 {int(cloud_estimate.get('dead') or 0)}"
            )
        return "\n".join(lines)

    def _cloud_report(self, settings: dict[str, Any]) -> dict[str, Any]:
        cloud = settings.get("cloud") if isinstance(settings.get("cloud"), dict) else {}
        if not bool(cloud.get("enabled")) or not str(cloud.get("server") or "").strip():
            return {}
        registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        payload = CloudClient(cloud, str(registration.get("proxy") or "")).capacity()
        return capacity_estimate(payload)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                settings = self.store.settings()
                notifications = settings.get("notifications") if isinstance(settings.get("notifications"), dict) else {}
                enabled = bool(notifications.get("bark_enabled", False))
                report_enabled = bool(notifications.get("bark_report_enabled", False))
                endpoint = str(notifications.get("bark_url") or "https://api.day.app").strip()
                key = str(notifications.get("bark_key") or "").strip()
                threshold = max(1, min(100000, int(notifications.get("bark_low_stock_threshold") or 100)))
                interval = max(
                    self.minimum_interval,
                    float(notifications.get("bark_check_interval_seconds") or 30),
                )
                report_interval = max(
                    60.0,
                    float(notifications.get("bark_report_interval_seconds") or 3600),
                )
                with self._lock:
                    self._state.update({
                        "enabled": enabled or report_enabled,
                        "report_enabled": report_enabled,
                        "report_interval_seconds": int(report_interval),
                    })
                if not report_enabled:
                    self._last_report_monotonic = 0.0
                elif self._last_report_monotonic <= 0:
                    self._last_report_monotonic = time.monotonic()
                mail = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
                provider = str(mail.get("provider") or "yyds").strip().lower()
                if not (enabled or report_enabled) or not key:
                    with self._lock:
                        self._state.update({
                            "enabled": enabled or report_enabled,
                            "state": "disabled" if not (enabled or report_enabled) else "waiting",
                            "threshold": threshold,
                            "last_error": "",
                            "last_report_error": "请在设置中填写 Bark Key" if report_enabled and not key else "",
                            "message": "请在设置中填写 Bark Key" if (enabled or report_enabled) else "Bark 通知未开启",
                        })
                    self._notified_low = False
                    self._wait(min(interval, report_interval))
                    continue

                split_limit = int(mail.get("outlook_split_limit") or 5)
                summary = OutlookMailboxPool(self.store.path("outlook_mailboxes.json")).summary(split_limit)
                available_slots = int(summary.get("available_slots") or 0)
                checked_at = _now()
                low_stock_enabled = enabled and provider == "outlook"
                with self._lock:
                    self._state.update({
                        "enabled": enabled or report_enabled,
                        "state": "low" if low_stock_enabled and available_slots < threshold else "normal",
                        "available_slots": available_slots,
                        "threshold": threshold,
                        "last_checked_at": checked_at,
                        "last_error": "",
                        "message": f"可注册量 {available_slots}，阈值 {threshold}" if low_stock_enabled and available_slots < threshold else f"可注册量 {available_slots}",
                    })
                if not low_stock_enabled or available_slots >= threshold:
                    self._notified_low = False
                elif not self._notified_low:
                    title = "GPT 注册号池库存不足"
                    body = f"Outlook 可注册量仅剩 {available_slots} 个，低于阈值 {threshold} 个。"
                    self._send(endpoint, key, title, body)
                    self._notified_low = True
                    with self._lock:
                        self._state["last_notified_at"] = checked_at
                        self._state["message"] = f"已通过 Bark 通知：可注册量 {available_slots}"
                    self.manager.log("warning", f"Bark 低库存通知已发送：Outlook 可注册量 {available_slots}，阈值 {threshold}")

                if report_enabled and time.monotonic() - self._last_report_monotonic >= report_interval:
                    try:
                        cloud_estimate = {}
                        try:
                            cloud_estimate = self._cloud_report(settings)
                        except Exception as exc:
                            self.manager.log("warning", f"Bark 汇报读取云端容量失败：{str(exc)[:300]}")
                        body = self._report_body(
                            provider,
                            summary,
                            self.manager.registration_report(),
                            cloud_estimate,
                        )
                        self._send(endpoint, key, "GPT 注册每小时汇报", body)
                        self._last_report_monotonic = time.monotonic()
                        with self._lock:
                            self._state["last_report_at"] = checked_at
                            self._state["last_report_error"] = ""
                        self.manager.log("success", "Bark 定时注册汇报已发送")
                    except Exception as exc:
                        detail = str(exc)[:500]
                        self._last_report_monotonic = time.monotonic()
                        with self._lock:
                            self._state["last_report_error"] = detail
                        self.manager.log("error", f"Bark 定时注册汇报失败：{detail}")
                self._last_error = ""
                wait_seconds = interval if low_stock_enabled else report_interval
                if report_enabled:
                    elapsed = max(0.0, time.monotonic() - self._last_report_monotonic)
                    wait_seconds = min(wait_seconds, max(0.5, report_interval - elapsed))
                self._wait(wait_seconds)
            except Exception as exc:
                detail = str(exc)[:500]
                with self._lock:
                    self._state["state"] = "error"
                    self._state["last_error"] = detail
                    self._state["message"] = detail
                if detail != self._last_error:
                    self.manager.log("error", f"Bark 低库存通知异常：{detail}")
                    self._last_error = detail
                self._wait(self.minimum_interval)
