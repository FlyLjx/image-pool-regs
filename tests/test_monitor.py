from __future__ import annotations

import time

from app.monitor import CloudRegistrationMonitor
from app.storage import JsonStore


class FakeManager:
    def __init__(self):
        self.state = "idle"
        self.starts = []
        self.logs = []

    def status(self, provider=None):
        assert provider in {None, "openai"}
        return {"state": self.state, "job_id": "job-1" if self.state == "running" else ""}

    def start(self, *, count, concurrency, provider="openai"):
        assert provider == "openai"
        self.starts.append((count, concurrency))
        self.state = "running"
        return {"job_id": "job-1", "state": "running"}

    def log(self, level, message):
        self.logs.append((level, message))


class FakeCloudClient:
    def __init__(self, _settings, _proxy):
        pass

    def capacity(self):
        return {
            "estimate": {
                "status": "shortage",
                "recommended_register_accounts": 3,
                "recommended_add_usable_accounts": 3,
                "current_effective_accounts": 7,
            }
        }


def test_monitor_confirms_shortage_then_starts_single_batch(tmp_path):
    store = JsonStore(tmp_path)
    settings = store.settings()
    settings["registration"]["concurrency"] = 2
    settings["cloud"].update({
        "enabled": True,
        "server": "https://cloud.example.test",
        "auth_key": "secret",
        "use_capacity": True,
        "monitor_enabled": True,
        "monitor_interval_seconds": 0.02,
        "monitor_concurrency": 2,
        "shortage_confirmations": 2,
        "monitor_batch_limit": 10,
    })
    store.write("settings.json", settings)
    manager = FakeManager()
    monitor = CloudRegistrationMonitor(
        store,
        manager,
        cloud_factory=FakeCloudClient,
        minimum_interval=0.01,
    )
    monitor.start()
    deadline = time.time() + 2
    while not manager.starts and time.time() < deadline:
        time.sleep(0.01)
    monitor.shutdown()

    assert manager.starts == [(3, 2)]
    assert monitor.status()["last_job_id"] == "job-1"
    assert any("1/2" in message for _level, message in manager.logs)


class LargeShortageCloudClient(FakeCloudClient):
    def capacity(self):
        return {
            "estimate": {
                "status": "shortage",
                "recommended_register_accounts": 8,
                "recommended_add_usable_accounts": 8,
                "current_effective_accounts": 2,
            }
        }


def test_monitor_uses_five_registration_workers_by_default(tmp_path):
    store = JsonStore(tmp_path)
    settings = store.settings()
    settings["cloud"].update({
        "enabled": True,
        "server": "https://cloud.example.test",
        "auth_key": "secret",
        "use_capacity": True,
        "monitor_enabled": True,
        "monitor_interval_seconds": 0.02,
        "shortage_confirmations": 1,
        "monitor_batch_limit": 10,
    })
    store.write("settings.json", settings)
    manager = FakeManager()
    monitor = CloudRegistrationMonitor(
        store,
        manager,
        cloud_factory=LargeShortageCloudClient,
        minimum_interval=0.01,
    )
    monitor.start()
    deadline = time.time() + 2
    while not manager.starts and time.time() < deadline:
        time.sleep(0.01)
    monitor.shutdown()

    assert manager.starts == [(8, 5)]


def test_monitor_toggle_is_persisted(tmp_path):
    store = JsonStore(tmp_path)
    monitor = CloudRegistrationMonitor(store, FakeManager(), minimum_interval=0.01)

    assert monitor.set_enabled(True)["enabled"] is True
    assert store.settings()["cloud"]["monitor_enabled"] is True
    assert monitor.set_enabled(False)["enabled"] is False
    assert store.settings()["cloud"]["monitor_enabled"] is False
