from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.registration.outlook import OutlookMailboxPool, OutlookMailClient
from app.storage import JsonStore


def test_login_protects_json_api(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "operator")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "test-password")
    app = create_app(store=JsonStore(tmp_path))

    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401

        failed = client.post("/api/auth/login", json={"username": "operator", "password": "wrong"})
        assert failed.status_code == 401

        logged_in = client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "test-password"},
        )
        assert logged_in.status_code == 200
        assert client.get("/api/auth/session").json()["username"] == "operator"
        assert client.get("/api/settings").status_code == 200

        client.post("/api/auth/logout")
        assert client.get("/api/settings").status_code == 401


def test_settings_validation_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    store = JsonStore(tmp_path)
    app = create_app(store=store)

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        settings = client.get("/api/settings").json()
        settings["registration"]["count"] = 4
        settings["registration"]["channel"] = "browser"
        settings["registration"]["browser_headless"] = True
        settings["registration"]["browser_slow_mo_ms"] = 75
        settings["mail"]["api_key"] = "AC-TEST"
        saved = client.put("/api/settings", json=settings)

        assert saved.status_code == 200
        assert store.settings()["registration"]["count"] == 4
        assert store.settings()["registration"]["channel"] == "browser"
        assert store.settings()["registration"]["browser_headless"] is True
        assert store.settings()["registration"]["browser_slow_mo_ms"] == 75
        assert store.settings()["mail"]["api_key"] == "AC-TEST"


def test_registration_concurrency_can_be_saved_up_to_fifty(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    store = JsonStore(tmp_path)
    app = create_app(store=store)

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        saved = client.put("/api/settings/registration/concurrency", json={"concurrency": 37})

        assert saved.status_code == 200
        assert saved.json()["concurrency"] == 37
        assert store.settings()["registration"]["concurrency"] == 37


def test_password_reveal_requires_login_and_reports_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    store = JsonStore(tmp_path)
    store.write(
        "accounts.json",
        [{
            "id": "account-1",
            "email": "one@example.test",
            "password": "Password123!",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": "id-token",
            "created_at": "2026-07-20T00:00:00+00:00",
        }],
    )
    app = create_app(store=store)

    with TestClient(app) as client:
        assert client.get("/api/accounts/account-1/credentials").status_code == 401
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})

        account = client.get("/api/accounts").json()["items"][0]
        credentials = client.get("/api/accounts/account-1/credentials").json()

        assert account["password"] == "********"
        assert account["has_refresh_token"] is True
        assert credentials["password"] == "Password123!"
        assert credentials["has_refresh_token"] is True


def test_outlook_pool_api_supports_api_key_json_import_and_authenticated_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("REG_OUTLOOK_IMPORT_API_KEY", "outlook-import-test-key")
    store = JsonStore(tmp_path)
    app = create_app(store=store)
    item = {
        "email": "pool@example.test",
        "password": "Password123!",
        "client_id": "client-id",
        "refresh_token": "refresh-token",
    }

    with TestClient(app) as client:
        assert client.post("/api/outlook-pool/import", json=[item]).status_code == 401
        imported = client.post(
            "/api/outlook-pool/import",
            headers={"x-api-key": "outlook-import-test-key"},
            json=[item],
        )
        assert imported.status_code == 200
        assert imported.json()["added"] == 1

        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        listing = client.get("/api/outlook-pool").json()
        assert listing["summary"]["total"] == 1
        assert listing["items"][0]["email"] == "pool@example.test"
        assert listing["import_api"]["api_key"] == "outlook-import-test-key"
        assert "refresh_token" not in listing["items"][0]


def test_authenticated_outlook_pool_delete_selected_and_clear_all(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    store = JsonStore(tmp_path)
    app = create_app(store=store)
    pool = OutlookMailboxPool(store.path("outlook_mailboxes.json"))
    for index in range(3):
        pool.import_payload({
            "email": f"pool{index}@example.test",
            "password": "Password123!",
            "client_id": f"client-{index}",
            "refresh_token": f"refresh-{index}",
        })

    with TestClient(app) as client:
        listing = pool.snapshot(5)
        selected_id = listing["items"][0]["id"]
        assert client.request("DELETE", "/api/outlook-pool", json={"mailbox_ids": [selected_id]}).status_code == 401

        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
        empty = client.request("DELETE", "/api/outlook-pool", json={"mailbox_ids": []})
        assert empty.status_code == 400

        deleted = client.request("DELETE", "/api/outlook-pool", json={"mailbox_ids": [selected_id]}).json()
        assert deleted["removed"] == 1
        assert deleted["total"] == 2

        cleared = client.request("DELETE", "/api/outlook-pool", json={"clear_all": True}).json()
        assert cleared["removed"] == 2
        assert cleared["total"] == 0


def test_outlook_mail_directory_counts_export_and_daily_api_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("REG_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("REG_ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("REG_OUTLOOK_IMPORT_API_KEY", "outlook-import-test-key")
    store = JsonStore(tmp_path)
    app = create_app(store=store)
    item = {
        "email": "mailbox@example.test",
        "password": "Password123!",
        "client_id": "client-id",
        "refresh_token": "refresh-token",
    }

    with TestClient(app) as client:
        imported = client.post(
            "/api/outlook-pool/import",
            headers={"x-api-key": "outlook-import-test-key"},
            json=[item],
        )
        assert imported.status_code == 200
        client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})

        listing = client.get("/api/outlook-mails").json()
        assert listing["total"] == 1
        assert listing["items"][0]["email"] == item["email"]
        assert listing["summary"]["mailbox_counted"] == 0
        assert listing["import_stats"]["today"]["api_added"] == 1

        detail = client.get("/api/outlook-mails/export.txt")
        assert detail.status_code == 200
        assert "mailbox@example.test----Password123!----client-id----refresh-token" in detail.text
        assert "Content-Disposition" in detail.headers
        raw = client.get("/api/outlook-mails/export.txt?format=raw")
        assert raw.status_code == 200
        assert "----available----" not in raw.text

        monkeypatch.setattr(OutlookMailClient, "count_messages", lambda self, record: 17)
        refreshed = client.post("/api/outlook-mails/refresh-counts", json={"all": True, "mailbox_ids": []})
        assert refreshed.status_code == 200
        assert refreshed.json()["updated"] == 1
        checked = client.get("/api/outlook-mails").json()
        assert checked["summary"]["mail_total"] == 17
        assert checked["items"][0]["mail_count"] == 17
