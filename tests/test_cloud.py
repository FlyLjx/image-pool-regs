from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import cloud as cloud_module
from app.cloud import CloudClient, capacity_estimate
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
    upload = client.upload_account({"email": "one@example.test"})

    assert capacity_estimate(capacity)["recommended_register_accounts"] == 3
    assert upload["added"] == 1
    assert options[0]["proxy"] == "http://127.0.0.1:7897"
    assert session.calls[0][0:2] == ("GET", "https://cloud.example.test/api/image-pool/capacity")
    assert session.calls[0][2]["params"] == {"limit": 60}
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer secret"
    assert session.calls[1][0:2] == ("POST", "https://cloud.example.test/api/accounts")
    assert session.calls[1][2]["json"]["accounts"][0]["email"] == "one@example.test"


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

