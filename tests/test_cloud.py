from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import cloud as cloud_module
from app.cloud import CloudClient, account_import_payload, capacity_estimate
from app.main import create_app
from app.manager import RegistrationManager
from app.storage import JsonStore


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = "response"

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)

    def close(self):
        return None


def test_cloud_capacity_and_account_upload_protocol(monkeypatch):
    session = FakeSession([
        FakeResponse({"estimate": {"status": "shortage", "recommended_register_accounts": 3}}),
        FakeResponse({"added": 1, "skipped": 0, "refreshed": 0}),
    ])
    options = []

    def session_factory(**kwargs):
        options.append(kwargs)
        return session

    monkeypatch.setattr(cloud_module.requests, "Session", session_factory)
    client = CloudClient(
        {
            "server": "https://cloud.example.test/",
            "auth_key": "secret",
            "capacity_limit": 60,
            "use_proxy": True,
        },
        "http://127.0.0.1:7897",
    )

    capacity = client.capacity()
    upload = client.upload_account({"email": "one@example.test", "access_token": "access-token"})

    assert capacity_estimate(capacity)["recommended_register_accounts"] == 3
    assert upload["added"] == 1
    assert options[0]["proxy"] == "http://127.0.0.1:7897"
    assert session.calls[0][0:2] == ("GET", "https://cloud.example.test/api/image-pool/capacity")
    assert session.calls[0][2]["params"] == {"limit": 60}
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer secret"
    assert session.calls[1][0:2] == ("POST", "https://cloud.example.test/api/accounts")
    assert session.calls[1][2]["json"]["accounts"][0]["email"] == "one@example.test"
    assert session.calls[1][2]["json"]["accounts"][0]["access_token"] == "access-token"


def test_account_import_payload_drops_local_runtime_fields():
    payload = account_import_payload(
        {
            "id": "account-1",
            "email": "one@example.test",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "health_status": "alive",
            "cloud_sync_status": "synced",
            "survival_total_seconds": 123,
        }
    )

    assert payload == {
        "id": "account-1",
        "email": "one@example.test",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
    }


def test_cloud_retries_transient_response_and_supports_flat_capacity(monkeypatch):
    session = FakeSession([
        FakeResponse({"error": {"message": "busy"}}, status_code=503),
        FakeResponse({"status": "shortage", "recommended_register_accounts": 4}),
    ])
    monkeypatch.setattr(cloud_module.requests, "Session", lambda **_kwargs: session)
    monkeypatch.setattr(cloud_module.time, "sleep", lambda _seconds: None)
    client = CloudClient(
        {
            "server": "https://cloud.example.test",
            "auth_key": "secret",
            "request_retries": 1,
        }
    )

    capacity = client.capacity()

    assert capacity_estimate(capacity)["status"] == "shortage"
    assert capacity_estimate(capacity)["recommended_register_accounts"] == 4
    assert len(session.calls) == 2


class ApiFakeRegistrar:
    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def register(self):
        return {
            "id": "cloud-test-account",
            "email": "cloud@example.test",
            "password": "Password123!",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def close(self):
        return None


def test_registration_reads_capacity_and_limits_batch(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    settings = store.settings()
    settings["mail"]["api_key"] = "AC-TEST"
    settings["cloud"].update({
        "enabled": True,
        "server": "https://cloud.example.test",
        "auth_key": "secret",
        "use_capacity": True,
        "upload_accounts": False,
    })
    store.write("settings.json", settings)
    manager = RegistrationManager(store, registrar_factory=ApiFakeRegistrar)

    class FakeCloudClient:
        def __init__(self, _settings, _proxy):
            pass

        def capacity(self):
            return {"estimate": {"status": "shortage", "recommended_register_accounts": 2}}

    monkeypatch.setattr("app.main.CloudClient", FakeCloudClient)
    app = create_app(store=store, manager=manager)

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        response = client.post("/api/registration/start", json={"count": 5, "concurrency": 1})
        assert response.status_code == 200
        assert response.json()["total"] == 2

        deadline = time.time() + 3
        while manager.status()["state"] == "running" and time.time() < deadline:
            time.sleep(0.02)
        assert manager.status()["success"] == 2

