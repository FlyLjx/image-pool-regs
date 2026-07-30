from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_SETTINGS: dict[str, Any] = {
    "registration": {
        "count": 1,
        "concurrency": 1,
        "provider": "openai",
        "channel": "protocol",
        "providers": {
            "openai": {"count": 1, "concurrency": 1},
        },
        "proxy": "",
        "browser_profile": "chrome_windows",
        "browser_engine": "camoufox",
        "browser_headless": False,
        "browser_slow_mo_ms": 40,
        "request_timeout": 45,
        "mail_wait_timeout": 120,
        "mail_poll_interval": 3,
    },
    "mail": {
        "provider": "yyds",
        "api_base": "https://maliapi.215.im/v1",
        "api_key": "",
        "domains": ["team.edu.yccc.me", "auto"],
        "email_prefix": "",
        "outlook_split_limit": 5,
    },
    "cloud": {
        "enabled": False,
        "server": "",
        "auth_key": "",
        "use_capacity": True,
        "capacity_limit": 60,
        "upload_accounts": True,
        "use_proxy": True,
        "monitor_enabled": False,
        "monitor_interval_seconds": 30,
        "monitor_concurrency": 5,
        "shortage_confirmations": 2,
        "monitor_batch_limit": 20,
    },
    "flaresolverr": {
        "enabled": False,
        "url": "http://flaresolverr:8191",
        "max_timeout_ms": 60000,
        "pass_proxy": True,
    },
    "sentinel": {
        "so_enabled": True,
        "so_required": False,
        "node": "node",
        "timeout_ms": 75000,
    },
    "health": {
        "auto_check_enabled": False,
        "interval_seconds": 300,
        "concurrency": 3,
        "recovery_concurrency": 3,
        "request_timeout": 30,
    },
}


def deep_merge(defaults: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], item)
        else:
            result[key] = copy.deepcopy(item)
    return result


class JsonStore:
    """Small thread-safe JSON store using atomic file replacement."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, name: str) -> Path:
        target = (self.root / name).resolve()
        target.relative_to(self.root)
        return target

    def read(self, name: str, default: Any) -> Any:
        path = self.path(name)
        with self._lock:
            if not path.exists():
                value = copy.deepcopy(default)
                self._write_unlocked(path, value)
                return value
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"JSON 文件读取失败: {path.name}: {exc}") from exc

    def write(self, name: str, value: Any) -> Any:
        path = self.path(name)
        with self._lock:
            self._write_unlocked(path, value)
        return value

    def update(self, name: str, default: Any, updater: Callable[[Any], Any]) -> Any:
        path = self.path(name)
        with self._lock:
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    raise RuntimeError(f"JSON 文件读取失败: {path.name}: {exc}") from exc
            else:
                current = copy.deepcopy(default)
            updated = updater(current)
            self._write_unlocked(path, updated)
            return updated

    @staticmethod
    def _write_unlocked(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        try:
            temp_path.write_text(payload, encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(temp_path, path)
                    break
                except PermissionError:
                    if attempt >= 4:
                        raise
                    time.sleep(0.04 * (attempt + 1))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def settings(self) -> dict[str, Any]:
        raw = self.read("settings.json", DEFAULT_SETTINGS)
        if not isinstance(raw, dict):
            raise RuntimeError("settings.json 必须是 JSON 对象")
        sanitized = copy.deepcopy(raw)
        sanitized.pop("adobe", None)
        sanitized.pop("adobe_delivery", None)
        sanitized.pop("codex_agent", None)
        sanitized.pop("sub2api", None)
        registration = sanitized.get("registration")
        if isinstance(registration, dict):
            registration["provider"] = "openai"
            providers = registration.get("providers")
            if isinstance(providers, dict):
                openai = providers.get("openai")
                registration["providers"] = {"openai": openai if isinstance(openai, dict) else {}}
        merged = deep_merge(DEFAULT_SETTINGS, sanitized)
        if merged != raw:
            self.write("settings.json", merged)
        return merged
