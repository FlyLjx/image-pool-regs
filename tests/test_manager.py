from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.manager import RegistrationManager
from app.storage import JsonStore


class FakeRegistrar:
    sequence = 0

    def __init__(self, _settings, logger, stop_event):
        type(self).sequence += 1
        self.number = type(self).sequence
        self.logger = logger
        self.stop_event = stop_event

    def register(self):
        self.logger("info", "mock protocol")
        return {
            "id": str(self.number),
            "email": f"account{self.number}@example.test",
            "password": "Password123!",
            "access_token": f"access-{self.number}",
            "refresh_token": f"refresh-{self.number}",
            "id_token": f"id-{self.number}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def close(self):
        return None


def wait_for_completion(manager: RegistrationManager) -> None:
    deadline = time.monotonic() + 3
    while manager.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.status()["state"] == "completed"


def test_manager_persists_each_success(tmp_path):
    FakeRegistrar.sequence = 0
    manager = RegistrationManager(JsonStore(tmp_path), registrar_factory=FakeRegistrar)
    manager.start(count=3, concurrency=2)
    wait_for_completion(manager)

    status = manager.status()
    accounts = manager.accounts(include_secrets=True)
    assert status["success"] == 3
    assert status["failed"] == 0
    assert set(status["providers"]) == {"openai"}
    assert len(accounts) == 3
    assert manager.accounts()[0]["password"] == "********"
    assert manager.account_credentials("1")["password"] == "Password123!"
    report = manager.registration_report()
    assert report["today"]["success"] == 3
    assert report["today"]["failed"] == 0
    assert report["today"]["success_rate"] == 100.0


def test_manager_uploads_successful_account_to_cloud(tmp_path, monkeypatch):
    FakeRegistrar.sequence = 0
    store = JsonStore(tmp_path)
    settings = store.settings()
    settings["cloud"].update({"enabled": True, "server": "https://cloud.example.test", "auth_key": "secret"})
    store.write("settings.json", settings)

    class FakeCloudClient:
        def __init__(self, _settings, _proxy):
            pass

        def upload_account(self, _account):
            return {"added": 1, "skipped": 0, "refreshed": 0}

    monkeypatch.setattr("app.manager.CloudClient", FakeCloudClient)
    manager = RegistrationManager(store, registrar_factory=FakeRegistrar)
    manager.start(count=1, concurrency=1)
    wait_for_completion(manager)

    account = manager.accounts(include_secrets=True)[0]
    assert account["cloud_sync_status"] == "synced"
    assert account["cloud_import_result"]["added"] == 1


def test_manager_hides_legacy_non_openai_accounts_without_deleting_them(tmp_path):
    store = JsonStore(tmp_path)
    store.write("accounts.json", [
        {"id": "legacy", "provider": "removed_provider", "email": "legacy@example.test"},
        {"id": "openai", "provider": "openai", "email": "openai@example.test"},
    ])
    manager = RegistrationManager(store)

    assert [item["id"] for item in manager.accounts()] == ["openai"]
    assert len(store.read("accounts.json", [])) == 2


def test_manager_rejects_removed_provider(tmp_path):
    manager = RegistrationManager(JsonStore(tmp_path))

    with pytest.raises(ValueError, match="ChatGPT"):
        manager.start(count=1, concurrency=1, provider="removed_provider")


def test_manager_dispatches_browser_channel_without_changing_provider(tmp_path):
    called: list[str] = []

    class BrowserFakeRegistrar(FakeRegistrar):
        def register(self):
            called.append("browser")
            account = super().register()
            account["source_type"] = "browser"
            return account

    class ProtocolMustNotRun(FakeRegistrar):
        def register(self):
            raise AssertionError("browser channel dispatched to protocol registrar")

    manager = RegistrationManager(
        JsonStore(tmp_path),
        registrar_factory=ProtocolMustNotRun,
        browser_registrar_factory=BrowserFakeRegistrar,
    )
    manager.start(count=1, concurrency=1, channel="browser")
    wait_for_completion(manager)

    status = manager.status()
    account = manager.accounts(include_secrets=True)[0]
    assert called == ["browser"]
    assert status["channel"] == "browser"
    assert account["provider"] == "openai"
    assert account["registration_channel"] == "browser"
    assert account["source_type"] == "browser"
