from __future__ import annotations

import copy
import imaplib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests

from app.registration.mail import _timestamp, extract_otp


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
MICROSOFT_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
VALID_POOL_STATUSES = {"available", "leased", "used", "failed"}
MAX_OUTLOOK_OTP_POLLS = 10


class OutlookOtpPollLimitError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OutlookMailboxPool:
    """Persistent, single-use Outlook mailbox pool shared by registration workers."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.path).lower()
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    @staticmethod
    def _normalize(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        email = str(item.get("email") or item.get("address") or "").strip().lower()
        client_id = str(item.get("client_id") or item.get("clientId") or "").strip()
        refresh_token = str(item.get("refresh_token") or item.get("refreshToken") or "").strip()
        # Both common mailbox exports are seen in practice. Repair rows where
        # the long MSA refresh token and UUID-shaped client id were reversed.
        if client_id.startswith("M.") and not refresh_token.startswith("M."):
            client_id, refresh_token = refresh_token, client_id
        if not email or "@" not in email or not client_id or not refresh_token:
            return None
        value = copy.deepcopy(item)
        value["id"] = str(value.get("id") or uuid.uuid4().hex)
        value["email"] = email
        value["password"] = str(value.get("password") or "")
        value["client_id"] = client_id
        value["refresh_token"] = refresh_token
        status = str(value.get("status") or "available").strip().lower()
        value["status"] = status if status in VALID_POOL_STATUSES else "available"
        value.setdefault("imported_at", _now())
        value.setdefault("updated_at", _now())
        return value

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Outlook 邮箱池读取失败: {exc}") from exc
        items = raw if isinstance(raw, list) else raw.get("mailboxes", []) if isinstance(raw, dict) else []
        return [value for item in items if (value := self._normalize(item)) is not None]

    def _write_unlocked(self, entries: list[dict[str, Any]]) -> None:
        payload = json.dumps(entries, ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _base_registered(item: dict[str, Any]) -> bool:
        return bool(str(item.get("base_registered_at") or "").strip() or item.get("base_registered") is True)

    @staticmethod
    def _lease_data(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        leases = item.get("split_leases") if isinstance(item.get("split_leases"), dict) else {}
        aliases = item.get("split_lease_aliases") if isinstance(item.get("split_lease_aliases"), dict) else {}
        return leases, aliases

    @classmethod
    def _base_leased(cls, item: dict[str, Any]) -> bool:
        leases, aliases = cls._lease_data(item)
        for lease_id, value in leases.items():
            if not str(value).lstrip("-").isdigit():
                continue
            slot = int(value)
            alias = str(aliases.get(lease_id) or "").strip()
            if slot == 0 or not alias:
                return True
        return False

    @classmethod
    def _alias_lease_slots(cls, item: dict[str, Any]) -> set[int]:
        leases, aliases = cls._lease_data(item)
        slots: set[int] = set()
        for lease_id, value in leases.items():
            if not str(value).lstrip("-").isdigit():
                continue
            slot = int(value)
            alias = str(aliases.get(lease_id) or "").strip()
            if slot > 0 and alias:
                slots.add(slot)
        return slots

    @staticmethod
    def _used_alias_slots(item: dict[str, Any]) -> set[int]:
        return {int(value) for value in item.get("used_split_slots", []) if str(value).isdigit() and int(value) > 0}

    @classmethod
    def _occupied_registration_count(cls, item: dict[str, Any]) -> int:
        return len(cls._used_alias_slots(item)) + len(cls._alias_lease_slots(item)) + (1 if cls._base_registered(item) or cls._base_leased(item) else 0)

    @classmethod
    def _summary(cls, entries: list[dict[str, Any]], split_limit: int = 5) -> dict[str, int]:
        summary = {
            "total": len(entries), "available": 0, "leased": 0, "used": 0, "failed": 0,
            "split_limit": max(1, int(split_limit or 5)), "available_slots": 0,
        }
        for item in entries:
            alias_lease_slots = cls._alias_lease_slots(item)
            base_leased = cls._base_leased(item)
            stored_status = str(item.get("status") or "available")
            status = "leased" if stored_status == "available" and (alias_lease_slots or base_leased) else stored_status
            summary[status if status in summary else "available"] += 1
            if stored_status == "available":
                used_alias_slots = cls._used_alias_slots(item)
                base_available = 0 if cls._base_registered(item) or base_leased else 1
                summary["available_slots"] += base_available + max(
                    0,
                    summary["split_limit"] - len(used_alias_slots) - len(alias_lease_slots),
                )
        return summary

    def summary(self, split_limit: int = 5) -> dict[str, int]:
        with self._lock:
            return self._summary(self._read_unlocked(), split_limit)

    def snapshot(
        self,
        split_limit: int = 5,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
        status: str = "all",
    ) -> dict[str, Any]:
        limit = max(1, min(50, int(split_limit or 5)))
        requested_page = max(1, int(page or 1))
        requested_size = max(10, min(100, int(page_size or 20)))
        search = str(query or "").strip().lower()
        category = str(status or "all").strip().lower()
        if category not in {"all", "available", "leased", "used", "failed"}:
            category = "all"
        with self._lock:
            entries = self._read_unlocked()
            summary = self._summary(entries, limit)
            items: list[dict[str, Any]] = []
            for item in entries:
                used_slots = sorted(self._used_alias_slots(item))
                leased_slots = sorted(self._alias_lease_slots(item))
                base_used = self._base_registered(item)
                base_leased = self._base_leased(item)
                stored_status = str(item.get("status") or "available")
                display_status = "leased" if stored_status == "available" and (leased_slots or base_leased) else stored_status
                email = str(item.get("email") or "")
                client_id = str(item.get("client_id") or "")
                if search and search not in f"{email} {client_id} {item.get('last_error') or ''}".lower():
                    continue
                if category != "all" and display_status != category:
                    continue
                items.append({
                    "id": str(item.get("id") or ""),
                    "email": email,
                    "client_id": client_id,
                    "status": display_status,
                    "base_used": base_used,
                    "base_leased": base_leased,
                    "used_splits": len(used_slots) + (1 if base_used else 0),
                    "leased_splits": len(leased_slots) + (1 if base_leased else 0),
                    "available_splits": (
                        (0 if base_used or base_leased else 1)
                        + max(0, limit - len(used_slots) - len(leased_slots))
                    ) if stored_status == "available" else 0,
                    "split_limit": limit,
                    "used_split_slots": used_slots,
                    "leased_split_slots": leased_slots,
                    "last_error": str(item.get("last_error") or ""),
                    "last_leased_at": str(item.get("last_leased_at") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                    "imported_at": str(item.get("imported_at") or ""),
                })
            # Keep the UI in mailbox add/import order. Runtime updates such as
            # leasing, failed checks, or token refresh should not move rows around.
            items.sort(key=lambda value: (value["imported_at"] or value["updated_at"], value["email"]))
            total = len(items)
            pages = max(1, (total + requested_size - 1) // requested_size)
            current_page = min(requested_page, pages)
            offset = (current_page - 1) * requested_size
            return {
                "items": items[offset:offset + requested_size],
                "page": current_page,
                "page_size": requested_size,
                "pages": pages,
                "total": total,
                "query": search,
                "status": category,
                "summary": summary,
            }

    def mailbox_records(self, mailbox_ids: list[str] | None = None, *, all_records: bool = False) -> list[dict[str, Any]]:
        """Return copies of full mailbox records for management operations."""
        targets = {str(value or "").strip() for value in (mailbox_ids or []) if str(value or "").strip()}
        with self._lock:
            entries = self._read_unlocked()
            if all_records:
                return copy.deepcopy(entries)
            if not targets:
                return []
            return copy.deepcopy([item for item in entries if str(item.get("id") or "") in targets])

    def export_text(self, *, detail: bool = True) -> str:
        """Export mailbox credentials in import-compatible order."""
        with self._lock:
            entries = self._read_unlocked()
        entries.sort(key=lambda item: (str(item.get("imported_at") or item.get("updated_at") or ""), str(item.get("email") or "")))
        lines: list[str] = []
        if detail:
            lines.append("# Outlook 邮箱管理导出：邮箱----密码----Client ID----Refresh Token----状态----添加时间")
        for item in entries:
            base = "----".join([
                str(item.get("email") or ""),
                str(item.get("password") or ""),
                str(item.get("client_id") or ""),
                str(item.get("refresh_token") or ""),
            ])
            if detail:
                base = "----".join([
                    base,
                    str(item.get("status") or "available"),
                    str(item.get("imported_at") or ""),
                ])
            lines.append(base)
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def parse_import(text: str) -> list[dict[str, Any]]:
        source = str(text or "").strip()
        if not source:
            return []
        if source.startswith("["):
            try:
                payload = json.loads(source)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Outlook 邮箱池 JSON 格式错误: {exc.msg}") from exc
            if not isinstance(payload, list):
                raise ValueError("Outlook 邮箱池 JSON 必须是数组")
            return [item for item in payload if isinstance(item, dict)]

        entries: list[dict[str, Any]] = []
        for line_number, line in enumerate(source.splitlines(), start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            pieces = [piece.strip() for piece in value.split("----")]
            if len(pieces) < 4:
                raise ValueError(f"Outlook 邮箱池第 {line_number} 行应为 邮箱----密码----Client ID----Refresh Token")
            entries.append({
                "email": pieces[0],
                "password": pieces[1],
                "client_id": pieces[2],
                "refresh_token": "----".join(pieces[3:]),
                "status": "available",
            })
        return entries

    def import_payload(self, payload: Any) -> dict[str, int]:
        source = payload
        if isinstance(source, dict) and "items" in source:
            source = source["items"]
        elif isinstance(source, dict) and "mailboxes" in source:
            source = source["mailboxes"]
        if isinstance(source, str):
            incoming = self.parse_import(source)
        elif isinstance(source, dict):
            incoming = [source]
        elif isinstance(source, list):
            incoming = [item for item in source if isinstance(item, dict)]
        else:
            raise ValueError("Outlook 邮箱池导入内容应为文本、对象或 JSON 数组")
        if not incoming:
            raise ValueError("没有可导入的 Outlook 邮箱")
        with self._lock:
            entries = self._read_unlocked()
            by_email = {str(item.get("email") or "").lower(): index for index, item in enumerate(entries)}
            added = 0
            updated = 0
            for raw in incoming:
                item = self._normalize(raw)
                if item is None:
                    raise ValueError("Outlook 邮箱需要邮箱、Client ID 和 Refresh Token")
                index = by_email.get(item["email"])
                if index is None:
                    entries.append(item)
                    by_email[item["email"]] = len(entries) - 1
                    added += 1
                    continue
                current = entries[index]
                item["id"] = str(current.get("id") or item["id"])
                item["status"] = str(current.get("status") or "available")
                item["last_error"] = str(current.get("last_error") or "")
                item["imported_at"] = str(current.get("imported_at") or item["imported_at"])
                item["updated_at"] = _now()
                entries[index] = item
                updated += 1
            self._write_unlocked(entries)
            return {**self._summary(entries), "added": added, "updated": updated}

    def import_text(self, text: str) -> dict[str, int]:
        return self.import_payload(text)

    def delete(self, mailbox_ids: list[str] | None = None, *, clear_all: bool = False) -> dict[str, int]:
        targets = {str(value or "").strip() for value in (mailbox_ids or []) if str(value or "").strip()}
        with self._lock:
            entries = self._read_unlocked()
            if clear_all:
                removed = len(entries)
                remaining: list[dict[str, Any]] = []
            elif targets:
                remaining = [item for item in entries if str(item.get("id") or "") not in targets]
                removed = len(entries) - len(remaining)
            else:
                remaining = entries
                removed = 0
            if removed:
                self._write_unlocked(remaining)
            return {**self._summary(remaining), "removed": removed}

    def delete_failed(self) -> dict[str, int]:
        """Remove only mailboxes explicitly marked as failed."""
        with self._lock:
            entries = self._read_unlocked()
            remaining = [item for item in entries if str(item.get("status") or "") != "failed"]
            removed = len(entries) - len(remaining)
            if removed:
                self._write_unlocked(remaining)
            return {**self._summary(remaining), "removed": removed}

    @staticmethod
    def _alias(email: str, tag: str) -> str:
        local, domain = str(email).rsplit("@", 1)
        return f"{local}+{tag}@{domain}"

    @staticmethod
    def _random_alias_tag(existing: set[str]) -> str:
        for _ in range(20):
            tag = uuid.uuid4().hex[:12]
            if tag not in existing:
                return tag
        raise RuntimeError("Outlook 随机分裂标签生成失败")

    @classmethod
    def _next_lease_target(cls, entries: list[dict[str, Any]], limit: int) -> tuple[dict[str, Any] | None, int, bool]:
        """Return the next mailbox/slot in import order without blocking other bases.

        Slot 1 is the base mailbox itself. Later slots are plus-address aliases.
        A base mailbox must be registered before its own aliases are issued, but
        a leased base is skipped so another imported base mailbox can register
        concurrently. Waiting is reserved for the case where every remaining
        slot is temporarily leased.
        """
        waiting_for_lease = False
        for item in entries:
            if item.get("status") != "available":
                continue
            used_slots = cls._used_alias_slots(item)
            alias_lease_slots = cls._alias_lease_slots(item)
            base_leased = cls._base_leased(item)
            # Mother/base mailbox is independent from split slots. Old data may
            # already contain used_split_slots=[1,2] from previous alias-only
            # behavior; without base_registered_at it must still start with the
            # real mother address.
            if not cls._base_registered(item):
                if base_leased:
                    waiting_for_lease = True
                    continue
                if not alias_lease_slots:
                    return item, 0, False
                waiting_for_lease = True
                continue
            if len(used_slots) >= limit:
                continue
            if alias_lease_slots:
                waiting_for_lease = True
                continue
            if 1 not in used_slots:
                return item, 1, False
            next_slot = next((slot for slot in range(2, limit + 1) if slot not in used_slots), 0)
            if next_slot:
                return item, next_slot, False
        return None, 0, waiting_for_lease

    def acquire(
        self,
        split_limit: int = 5,
        *,
        wait_timeout: float = 0.0,
        stopped: Callable[[], bool] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(50, int(split_limit or 5)))
        deadline = time.monotonic() + max(0.0, float(wait_timeout or 0.0))
        waiting_for_current = False
        last_status_at = 0.0
        while True:
            if stopped and stopped():
                raise RuntimeError("任务已停止")
            with self._lock:
                entries = self._read_unlocked()
                chosen, split_index, waiting_for_current = self._next_lease_target(entries, limit)
                if chosen is not None:
                    lease_id = uuid.uuid4().hex
                    leases = chosen.get("split_leases") if isinstance(chosen.get("split_leases"), dict) else {}
                    lease_aliases = (
                        chosen.get("split_lease_aliases")
                        if isinstance(chosen.get("split_lease_aliases"), dict)
                        else {}
                    )
                    used_aliases = (
                        chosen.get("used_split_aliases")
                        if isinstance(chosen.get("used_split_aliases"), dict)
                        else {}
                    )
                    existing_aliases = {
                        str(value).strip().lower()
                        for value in (*lease_aliases.values(), *used_aliases.values())
                        if str(value).strip()
                    }
                    split_alias = "" if split_index == 0 else self._random_alias_tag(existing_aliases)
                    leases[lease_id] = split_index
                    lease_aliases[lease_id] = split_alias
                    chosen["split_leases"] = leases
                    chosen["split_lease_aliases"] = lease_aliases
                    chosen["last_leased_at"] = _now()
                    chosen["updated_at"] = _now()
                    self._write_unlocked(entries)
                    assigned = copy.deepcopy(chosen)
                    assigned["lease_id"] = lease_id
                    assigned["split_index"] = split_index
                    assigned["split_alias"] = split_alias
                    assigned["registered_email"] = (
                        str(chosen["email"]) if split_index == 0 else self._alias(str(chosen["email"]), split_alias)
                    )
                    return assigned
            if not waiting_for_current:
                raise RuntimeError(f"Outlook 邮箱池没有可用分裂邮箱（每个基础邮箱上限 {limit} 个）")
            now = time.monotonic()
            if on_status and (last_status_at <= 0 or now - last_status_at >= 15.0):
                last_status_at = now
                on_status("Outlook 号池暂时没有空闲租约，等待任一注册任务释放地址")
            if time.monotonic() >= deadline:
                raise RuntimeError("Outlook 号池等待租约释放超时")
            if stopped and stopped():
                raise RuntimeError("任务已停止")
            time.sleep(1.0)

    def find(self, email: str) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        with self._lock:
            for item in self._read_unlocked():
                if str(item.get("email") or "").lower() == target:
                    return copy.deepcopy(item)
        raise RuntimeError("Outlook 邮箱池未找到该邮箱")

    def release(self, mailbox: dict[str, Any], *, used: bool = False, error: str = "") -> None:
        identifier = str(mailbox.get("id") or "").strip()
        lease_id = str(mailbox.get("lease_id") or "").strip()
        if not identifier:
            return
        with self._lock:
            entries = self._read_unlocked()
            for item in entries:
                if str(item.get("id") or "") != identifier:
                    continue
                leases = item.get("split_leases") if isinstance(item.get("split_leases"), dict) else {}
                if lease_id and lease_id not in leases:
                    return
                lease_aliases = (
                    item.get("split_lease_aliases")
                    if isinstance(item.get("split_lease_aliases"), dict)
                    else {}
                )
                split_index = int(leases.pop(lease_id, mailbox.get("split_index") or 0) or 0)
                split_alias = str(lease_aliases.pop(lease_id, mailbox.get("split_alias") or "") or "").strip()
                if used and (split_index == 0 or not split_alias):
                    item["base_registered"] = True
                    item["base_registered_at"] = _now()
                    item["base_registered_email"] = str(item.get("email") or mailbox.get("address") or "")
                elif used and split_index > 0:
                    used_slots = {int(value) for value in item.get("used_split_slots", []) if str(value).isdigit()}
                    used_slots.add(split_index)
                    item["used_split_slots"] = sorted(used_slots)
                    if split_alias:
                        used_aliases = (
                            item.get("used_split_aliases")
                            if isinstance(item.get("used_split_aliases"), dict)
                            else {}
                        )
                        used_aliases[str(split_index)] = split_alias
                        item["used_split_aliases"] = used_aliases
                    split_limit = max(1, int(mailbox.get("split_limit") or 5))
                    if len(used_slots) >= split_limit:
                        item["status"] = "used"
                item["split_leases"] = leases
                item["split_lease_aliases"] = lease_aliases
                item["last_error"] = str(error or "")[:500]
                item["updated_at"] = _now()
                self._write_unlocked(entries)
                return

    def clear_leases(self) -> int:
        cleared = 0
        with self._lock:
            entries = self._read_unlocked()
            for item in entries:
                leases = item.get("split_leases") if isinstance(item.get("split_leases"), dict) else {}
                lease_aliases = item.get("split_lease_aliases") if isinstance(item.get("split_lease_aliases"), dict) else {}
                if leases or lease_aliases:
                    item["split_leases"] = {}
                    item["split_lease_aliases"] = {}
                    item["updated_at"] = _now()
                    cleared += 1
            if cleared:
                self._write_unlocked(entries)
        return cleared

    def mark_failed(self, mailbox: dict[str, Any], error: str) -> None:
        identifier = str(mailbox.get("id") or "").strip()
        if not identifier:
            return
        with self._lock:
            entries = self._read_unlocked()
            for item in entries:
                if str(item.get("id") or "") != identifier:
                    continue
                item["status"] = "failed"
                item["last_error"] = str(error or "")[:500]
                item["updated_at"] = _now()
                item["split_leases"] = {}
                item["split_lease_aliases"] = {}
                self._write_unlocked(entries)
                return

    def update_refresh_token(self, mailbox: dict[str, Any], refresh_token: str) -> None:
        identifier = str(mailbox.get("id") or "").strip()
        token = str(refresh_token or "").strip()
        if not identifier or not token:
            return
        with self._lock:
            entries = self._read_unlocked()
            for item in entries:
                if str(item.get("id") or "") == identifier:
                    item["refresh_token"] = token
                    item["updated_at"] = _now()
                    self._write_unlocked(entries)
                    return


class OutlookMailClient:
    provider_name = "outlook"

    def __init__(
        self,
        pool_path: str | Path,
        *,
        proxy: str = "",
        request_timeout: float = 30,
        split_limit: int = 5,
        queue_wait_timeout: float = 1800,
        stopped: Callable[[], bool] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.pool = OutlookMailboxPool(pool_path)
        self.request_timeout = max(10.0, float(request_timeout or 30))
        self.split_limit = max(1, min(50, int(split_limit or 5)))
        self.queue_wait_timeout = max(300.0, float(queue_wait_timeout or 1800))
        self.stopped = stopped
        self.on_status = on_status
        options: dict[str, Any] = {"impersonate": "chrome", "verify": False}
        if proxy:
            options["proxy"] = proxy
        self.session = requests.Session(**options)
        self._leased: dict[str, Any] | None = None

    def close(self) -> None:
        if self._leased is not None:
            self.pool.release(self._leased)
            self._leased = None
        self.session.close()

    @staticmethod
    def _mailbox(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "address": str(record.get("registered_email") or record["email"]),
            "base_address": str(record["email"]),
            "id": str(record["id"]),
            "lease_id": str(record.get("lease_id") or ""),
            "split_index": int(record.get("split_index") or 0),
            "split_alias": str(record.get("split_alias") or ""),
            "split_limit": int(record.get("split_limit") or 5),
            "client_id": str(record["client_id"]),
            "refresh_token": str(record["refresh_token"]),
            "tenant": str(record.get("tenant") or ""),
            "token_url": str(record.get("token_url") or ""),
        }

    def create_mailbox(self, _domain: str | None = None, _local_part: str | None = None) -> dict[str, Any]:
        self._leased = self.pool.acquire(
            self.split_limit,
            wait_timeout=self.queue_wait_timeout,
            stopped=self.stopped,
            on_status=self.on_status,
        )
        self._leased["split_limit"] = self.split_limit
        return self._mailbox(self._leased)

    def existing_mailbox(self, email: str) -> dict[str, Any]:
        address = str(email or "").strip().lower()
        base = address.split("+", 1)[0] + "@" + address.rsplit("@", 1)[-1] if "+" in address and "@" in address else address
        return self._mailbox(self.pool.find(base))

    def commit_mailbox(self, mailbox: dict[str, Any]) -> None:
        if self._leased is None:
            return
        self.pool.release(mailbox, used=True)
        self._leased = None

    def fail_mailbox(self, mailbox: dict[str, Any], error: str) -> None:
        self.pool.mark_failed(mailbox, error)
        if self._leased is not None and str(self._leased.get("id") or "") == str(mailbox.get("id") or ""):
            self._leased = None

    def _access_token(
        self,
        mailbox: dict[str, Any],
        *,
        refresh: bool = False,
        scope: str | None = GRAPH_DEFAULT_SCOPE,
    ) -> str:
        token_prefix = "graph" if scope == GRAPH_DEFAULT_SCOPE else "outlook"
        token_key = f"{token_prefix}_access_token"
        expires_key = f"{token_prefix}_access_token_expires_at"
        scope_key = f"{token_prefix}_access_token_scope"
        token = str(mailbox.get(token_key) or "").strip()
        expires_at = float(mailbox.get(expires_key) or 0)
        if token and not refresh and expires_at > time.time() + 60:
            return token
        explicit_url = str(mailbox.get("token_url") or "").strip()
        tenant = str(mailbox.get("tenant") or "").strip().strip("/")
        tenant_url = (
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
            if tenant else ""
        )
        token_urls = []
        for url in (explicit_url, tenant_url, MICROSOFT_COMMON_TOKEN_URL, MICROSOFT_CONSUMERS_TOKEN_URL):
            if url and url not in token_urls:
                token_urls.append(url)

        errors: list[tuple[int, str]] = []
        response = None
        data: dict[str, Any] = {}
        for token_url in token_urls:
            request_data = {
                "client_id": str(mailbox.get("client_id") or ""),
                "grant_type": "refresh_token",
                "refresh_token": str(mailbox.get("refresh_token") or ""),
            }
            if scope:
                request_data["scope"] = scope
            response = self.session.request(
                "POST",
                token_url,
                data=request_data,
                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
                timeout=self.request_timeout,
                verify=False,
            )
            try:
                payload = response.json() if response.text else {}
                data = payload if isinstance(payload, dict) else {}
            except Exception:
                data = {}
            if response.status_code == 200 and str(data.get("access_token") or "").strip():
                break
            detail = str(data.get("error_description") or data.get("error") or response.text[:240]).strip()
            errors.append((int(response.status_code), detail))
        else:
            status = errors[-1][0] if errors else 0
            detail = " | ".join(message for _status, message in errors if message)[:800]
            lowered = detail.lower()
            tenant_mismatch = "aadsts7000012" in lowered or "different tenant" in lowered
            if "invalid_grant" in lowered and not tenant_mismatch:
                self.pool.mark_failed(mailbox, detail)
            raise RuntimeError(f"Outlook OAuth 授权失败: HTTP {status}: {detail}")

        assert response is not None
        refreshed = str(data.get("refresh_token") or "").strip()
        if refreshed:
            mailbox["refresh_token"] = refreshed
            self.pool.update_refresh_token(mailbox, refreshed)
        mailbox[token_key] = str(data["access_token"])
        mailbox[expires_key] = time.time() + max(60, int(data.get("expires_in") or 3600))
        mailbox[scope_key] = str(data.get("scope") or "")
        mailbox["access_token_scope"] = mailbox[scope_key]
        return mailbox[token_key]

    def _graph_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        response = self.session.request(
            "GET",
            f"{GRAPH_BASE}/me/messages",
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/json",
                "prefer": 'outlook.body-content-type="text"',
            },
            params={
                "$top": "25",
                "$select": "id,subject,body,bodyPreview,receivedDateTime",
                "$orderby": "receivedDateTime desc",
            },
            timeout=self.request_timeout,
            verify=False,
        )
        try:
            data = response.json() if response.text else {}
        except Exception:
            data = {}
        if response.status_code != 200:
            detail = str(data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else response.text[:240]).strip()
            raise RuntimeError(f"Outlook Graph 读取邮件失败: HTTP {response.status_code}: {detail}")
        values = data.get("value") if isinstance(data, dict) else []
        messages: list[dict[str, Any]] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            body = item.get("body") if isinstance(item.get("body"), dict) else {}
            messages.append({
                "id": str(item.get("id") or ""),
                "subject": str(item.get("subject") or ""),
                "text": str(body.get("content") or item.get("bodyPreview") or ""),
                "receivedDateTime": str(item.get("receivedDateTime") or ""),
            })
        return messages

    @staticmethod
    def _parsed_imap_message(message_id: bytes, raw: bytes) -> dict[str, Any]:
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        bodies: list[str] = []
        parts = parsed.walk() if parsed.is_multipart() else (parsed,)
        for part in parts:
            if part.get_content_maintype() != "text" or part.get_content_disposition() == "attachment":
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else ""
            if content:
                bodies.append(str(content))
        received_at = ""
        try:
            value = parsedate_to_datetime(str(parsed.get("date") or ""))
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                received_at = value.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
        return {
            "id": str(parsed.get("message-id") or message_id.decode(errors="ignore")),
            "subject": str(parsed.get("subject") or ""),
            "text": "\n".join(bodies),
            "receivedDateTime": received_at,
        }

    def _imap_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        address = str(mailbox.get("base_address") or mailbox.get("address") or "").strip()
        auth = f"user={address}\x01auth=Bearer {access_token}\x01\x01".encode()
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL("outlook.office365.com", 993, timeout=self.request_timeout)
            client.authenticate("XOAUTH2", lambda _challenge: auth)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Outlook IMAP 打开收件箱失败")
            status, result = client.search(None, "ALL")
            if status != "OK":
                raise RuntimeError("Outlook IMAP 搜索邮件失败")
            identifiers = (result[0].split() if result and result[0] else [])[-25:]
            messages: list[dict[str, Any]] = []
            for message_id in reversed(identifiers):
                status, payload = client.fetch(message_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((item[1] for item in payload if isinstance(item, tuple) and isinstance(item[1], bytes)), b"")
                if raw:
                    messages.append(self._parsed_imap_message(message_id, raw))
            return messages
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(f"Outlook IMAP OAuth 读取邮件失败: {str(exc)[:300]}") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

    def _messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        last_error = ""
        for attempt in range(2):
            try:
                graph_token = self._access_token(
                    mailbox,
                    refresh=attempt > 0,
                    scope=GRAPH_DEFAULT_SCOPE,
                )
                return self._graph_messages(mailbox, graph_token)
            except RuntimeError as exc:
                last_error = str(exc)
            try:
                outlook_token = self._access_token(
                    mailbox,
                    refresh=attempt > 0,
                    scope=None,
                )
                outlook_scope = str(mailbox.get("outlook_access_token_scope") or "").lower()
                if "imap.accessasuser.all" not in outlook_scope:
                    raise RuntimeError(
                        "Outlook OAuth Token 缺少 IMAP 权限"
                        f"（scope={outlook_scope or 'unknown'}）"
                    )
                return self._imap_messages(mailbox, outlook_token)
            except RuntimeError as exc:
                last_error = f"Graph: {last_error}; IMAP: {str(exc)}"
                if attempt == 0:
                    continue
                if "authenticated but not connected" in last_error.lower():
                    self.pool.mark_failed(mailbox, last_error)
                raise RuntimeError(last_error) from exc
        raise RuntimeError(last_error or "Outlook 邮件读取失败")

    def existing_codes(self, mailbox: dict[str, Any], limit: int = 50) -> set[str]:
        codes: set[str] = set()
        for item in self._messages(mailbox)[: max(1, int(limit))]:
            code = extract_otp(item)
            if code:
                codes.add(code)
        return codes

    def wait_for_code(
        self,
        mailbox: dict[str, Any],
        *,
        requested_at: float,
        timeout: float,
        interval: float,
        stopped: Callable[[], bool] | None = None,
        excluded_codes: set[str] | None = None,
        on_status: Callable[[str], None] | None = None,
        max_polls: int | None = MAX_OUTLOOK_OTP_POLLS,
    ) -> str | None:
        deadline = time.monotonic() + max(10.0, timeout)
        poll_limit = max(1, int(max_polls)) if max_polls is not None else 0
        seen: set[str] = set()
        excluded = {str(code).strip() for code in (excluded_codes or set()) if str(code).strip()}
        poll_count = 0
        last_status_at = 0.0
        while time.monotonic() < deadline:
            if stopped and stopped():
                raise RuntimeError("任务已停止")
            messages = self._messages(mailbox)
            poll_count += 1
            for item in messages:
                identity = str(item.get("id") or repr(sorted(item.items())))
                if identity in seen:
                    continue
                seen.add(identity)
                received_at = _timestamp(item.get("receivedDateTime"))
                if received_at and received_at < requested_at - 3:
                    continue
                code = extract_otp(item)
                if code and code not in excluded:
                    return code
            now = time.monotonic()
            should_report = poll_count == 1 or now - last_status_at >= 15.0
            if poll_limit and poll_count >= poll_limit:
                message = (
                    f"Outlook 邮箱轮询已达 {poll_limit} 次仍未收到新验证码，"
                    "当前母号标记失效并切换下一个号"
                )
                if on_status:
                    on_status(message)
                raise OutlookOtpPollLimitError(message)
            if on_status and should_report:
                last_status_at = now
                on_status(f"Outlook 邮箱轮询第 {poll_count} 次：读取到 {len(messages)} 封邮件，尚未发现新的验证码")
            time.sleep(max(0.5, interval))
        return None
