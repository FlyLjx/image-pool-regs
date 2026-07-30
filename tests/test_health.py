from __future__ import annotations

import threading
import time

from app.health import AccountHealthChecker, AccountHealthService, classify_health, cloudflare_challenge
from app.manager import RegistrationManager
from app.storage import JsonStore


def settings():
    return {
        "registration": {"proxy": "", "browser_profile": "chrome_windows"},
        "mail": {"api_key": "AC-TEST"},
        "sentinel": {},
        "flaresolverr": {},
        "health": {"request_timeout": 30},
    }


class SequenceHealthChecker(AccountHealthChecker):
    def __init__(self, responses, **kwargs):
        super().__init__(settings(), **kwargs)
        self.responses = iter(responses)

    def _probe(self, _access_token):
        return next(self.responses)


class FakeRegistrar:
    calls = 0

    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def relogin(self, email, password):
        type(self).calls += 1
        assert email == "one@example.test"
        assert password == "Password123!"
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def close(self):
        return None


class FakeRefreshRegistrar(FakeRegistrar):
    refresh_calls = 0

    def refresh(self, refresh_token):
        type(self).refresh_calls += 1
        assert refresh_token == "old-refresh"
        return {
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "id_token": "refreshed-id",
        }


def account():
    return {
        "id": "account-1",
        "email": "one@example.test",
        "password": "Password123!",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
    }


def test_health_classification_distinguishes_expired_and_banned():
    assert classify_health(200, "ok")["status"] == "alive"
    assert classify_health(401, "expired")["status"] == "expired"
    assert classify_health(403, "account_deactivated")["status"] == "banned"


def test_cloudflare_challenge_is_an_environment_status():
    body = '<html><script>window._cf_chl_opt = {}</script>Enable JavaScript and cookies to continue</html>'

    assert cloudflare_challenge(403, body) is True
    assert classify_health(403, body)["status"] == "environment"
    assert classify_health(403, '{"detail":"forbidden"}')["status"] == "restricted"


def test_health_request_sends_browser_navigation_headers():
    captured = {}

    class Session:
        def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return object()

    checker = AccountHealthChecker(settings())
    checker._health_request(Session(), "access-token")

    headers = captured["headers"]
    assert captured["url"] == "https://chatgpt.com/backend-api/me"
    assert headers["authorization"] == "Bearer access-token"
    assert headers["origin"] == "https://chatgpt.com"
    assert headers["referer"] == "https://chatgpt.com/"
    assert headers["sec-ch-ua-platform"] == '"Windows"'


def test_401_uses_password_relogin_and_saves_new_tokens():
    FakeRegistrar.calls = 0
    checker = SequenceHealthChecker(
        [(401, "expired"), (200, "ok")],
        registrar_factory=FakeRegistrar,
    )

    result = checker.check(account())

    assert FakeRegistrar.calls == 1
    assert result["recovered"] is True
    assert result["updates"]["health_status"] == "alive"
    assert result["updates"]["health_recovery_status"] == "recovered"
    assert result["updates"]["access_token"] == "new-access"
    assert result["updates"]["refresh_token"] == "new-refresh"


def test_401_uses_refresh_token_before_password_relogin():
    FakeRegistrar.calls = 0
    FakeRefreshRegistrar.calls = 0
    FakeRefreshRegistrar.refresh_calls = 0
    checker = SequenceHealthChecker(
        [(401, "expired"), (200, "ok")],
        registrar_factory=FakeRefreshRegistrar,
    )

    result = checker.check(account())

    assert FakeRefreshRegistrar.refresh_calls == 1
    assert FakeRefreshRegistrar.calls == 0
    assert result["recovered"] is True
    assert result["updates"]["health_recovery_status"] == "recovered"
    assert result["updates"]["access_token"] == "refreshed-access"
    assert result["updates"]["refresh_token"] == "refreshed-refresh"


def test_confirmed_ban_never_attempts_password_relogin():
    FakeRegistrar.calls = 0
    checker = SequenceHealthChecker(
        [(403, "account_deactivated")],
        registrar_factory=FakeRegistrar,
    )

    result = checker.check(account())

    assert FakeRegistrar.calls == 0
    assert result["updates"]["health_status"] == "banned"
    assert result["updates"]["disabled"] is True


class BlockingChecker:
    started = threading.Event()
    release = threading.Event()

    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def check(self, _account):
        type(self).started.set()
        type(self).release.wait(timeout=2)
        return {
            "updates": {
                "health_status": "alive",
                "health_alive": True,
                "health_checked_at": "2026-07-20T00:00:00+00:00",
                "health_detail": "ok",
            },
            "recovered": False,
        }


def test_health_service_persists_checking_before_final_result(tmp_path):
    BlockingChecker.started.clear()
    BlockingChecker.release.clear()
    store = JsonStore(tmp_path)
    store.write("accounts.json", [account()])
    manager = RegistrationManager(store)
    service = AccountHealthService(store, manager, checker_factory=BlockingChecker)

    service.start_check(["account-1"])
    assert BlockingChecker.started.wait(timeout=1)
    assert store.read("accounts.json", [])[0]["health_status"] == "checking"
    BlockingChecker.release.set()
    deadline = time.time() + 2
    while service.status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)

    saved = store.read("accounts.json", [])[0]
    assert saved["health_status"] == "alive"
    assert service.status()["checked"] == 1


class ConcurrentManualChecker:
    lock = threading.Lock()
    active = 0
    max_active = 0
    both_started = threading.Event()
    release = threading.Event()

    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def check(self, _account):
        with type(self).lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            if type(self).active >= 2:
                type(self).both_started.set()
        type(self).release.wait(timeout=2)
        with type(self).lock:
            type(self).active -= 1
        return {
            "updates": {
                "health_status": "alive",
                "health_alive": True,
                "health_checked_at": "2026-07-20T00:00:00+00:00",
                "health_detail": "ok",
            },
            "recovered": False,
        }


def test_manual_account_checks_join_running_batch_concurrently(tmp_path):
    ConcurrentManualChecker.active = 0
    ConcurrentManualChecker.max_active = 0
    ConcurrentManualChecker.both_started.clear()
    ConcurrentManualChecker.release.clear()
    store = JsonStore(tmp_path)
    store.write("settings.json", {"health": {"concurrency": 2, "recovery_concurrency": 1}})
    accounts = [{**account(), "id": f"account-{index}"} for index in range(2)]
    store.write("accounts.json", accounts)
    service = AccountHealthService(store, RegistrationManager(store), checker_factory=ConcurrentManualChecker)

    service.start_check(["account-0"])
    deadline = time.time() + 1
    while ConcurrentManualChecker.active < 1 and time.time() < deadline:
        time.sleep(0.01)
    joined = service.start_check(["account-1"])

    assert joined["total"] == 2
    assert ConcurrentManualChecker.both_started.wait(timeout=1)
    assert ConcurrentManualChecker.max_active == 2
    ConcurrentManualChecker.release.set()
    deadline = time.time() + 2
    while service.status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert service.status()["checked"] == 2


class ConcurrentRecoveryChecker:
    lock = threading.Lock()
    active = 0
    max_active = 0

    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def probe(self, _account):
        initial = {"status": "expired", "alive": False, "detail": "expired"}
        return {
            "updates": {"health_status": "expired", "health_checked_at": "2026-07-20T00:00:00+00:00"},
            "recovered": False,
            "needs_recovery": True,
            "initial_result": initial,
            "initial_http_status": 401,
        }

    def recover(self, _account, _probe_result):
        with type(self).lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        time.sleep(0.06)
        with type(self).lock:
            type(self).active -= 1
        return {
            "updates": {
                "health_status": "alive",
                "health_alive": True,
                "health_checked_at": "2026-07-20T01:00:00+00:00",
                "health_recovery_status": "recovered",
            },
            "recovered": True,
        }


def test_recovery_uses_its_own_concurrent_worker_pool(tmp_path):
    ConcurrentRecoveryChecker.active = 0
    ConcurrentRecoveryChecker.max_active = 0
    store = JsonStore(tmp_path)
    store.write("settings.json", {"health": {"concurrency": 3, "recovery_concurrency": 2}})
    accounts = [{**account(), "id": f"account-{index}", "email": f"account-{index}@example.test"} for index in range(3)]
    store.write("accounts.json", accounts)
    service = AccountHealthService(store, RegistrationManager(store), checker_factory=ConcurrentRecoveryChecker)

    service.start_check()
    deadline = time.time() + 3
    while service.status()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)

    status = service.status()
    assert status["checked"] == 3
    assert status["recovered"] == 3
    assert status["recovery_concurrency"] == 2
    assert ConcurrentRecoveryChecker.max_active == 2


class CancellableRecoveryChecker:
    recovery_started = threading.Event()
    recovery_calls = 0
    lock = threading.Lock()

    def __init__(self, _settings, logger, stop_event):
        self.logger = logger
        self.stop_event = stop_event

    def probe(self, _account):
        initial = {"status": "expired", "alive": False, "detail": "expired"}
        return {
            "updates": {"health_status": "expired", "health_checked_at": "2026-07-20T00:00:00+00:00"},
            "recovered": False,
            "needs_recovery": True,
            "initial_result": initial,
            "initial_http_status": 401,
        }

    def recover(self, _account, _probe_result):
        with type(self).lock:
            type(self).recovery_calls += 1
        self.logger("info", "等待登录验证码")
        type(self).recovery_started.set()
        assert self.stop_event.wait(timeout=2)
        raise RuntimeError("stopped")


def test_current_recovery_batch_can_be_stopped_without_stopping_monitor(tmp_path):
    CancellableRecoveryChecker.recovery_started.clear()
    CancellableRecoveryChecker.recovery_calls = 0
    store = JsonStore(tmp_path)
    store.write("settings.json", {
        "health": {"auto_check_enabled": False, "concurrency": 2, "recovery_concurrency": 1},
    })
    accounts = [{**account(), "id": f"account-{index}"} for index in range(2)]
    store.write("accounts.json", accounts)
    service = AccountHealthService(
        store,
        RegistrationManager(store),
        checker_factory=CancellableRecoveryChecker,
        minimum_interval=10,
    )
    service.start()

    service.start_check()
    assert CancellableRecoveryChecker.recovery_started.wait(timeout=1)
    running = service.status()
    assert running["recovery_active"] == 1
    assert running["recovery_stage_counts"] == {"otp": 1}
    assert running["recovery_items"][0]["stage_label"] == "等待/校验验证码"
    stopping = service.stop_check()
    assert stopping["state"] == "stopping"

    deadline = time.time() + 2
    while service.status()["state"] == "stopping" and time.time() < deadline:
        time.sleep(0.01)

    status = service.status()
    assert status["state"] == "cancelled"
    assert status["thread_alive"] is True
    assert status["auto_enabled"] is False
    assert CancellableRecoveryChecker.recovery_calls == 1
    assert all(item["health_status"] == "unchecked" for item in store.read("accounts.json", []))
    assert all(item["health_detail"] == "本轮检测/恢复已停止" for item in store.read("accounts.json", []))
    service.shutdown()


def test_recovery_closes_previous_survival_period_and_resets_start(tmp_path):
    store = JsonStore(tmp_path)
    saved = {
        **account(),
        "created_at": "2026-07-20T00:00:00+00:00",
        "survival_started_at": "2026-07-20T00:00:00+00:00",
    }
    store.write("accounts.json", [saved])
    service = AccountHealthService(store, RegistrationManager(store))

    service._update_account(saved, {
        "health_status": "expired",
        "health_checked_at": "2026-07-20T01:00:00+00:00",
    })
    service._update_account(saved, {
        "health_status": "alive",
        "health_alive": True,
        "health_checked_at": "2026-07-20T02:00:00+00:00",
        "health_recovery_status": "recovered",
    })

    updated = store.read("accounts.json", [])[0]
    assert updated["survival_started_at"] == "2026-07-20T02:00:00+00:00"
    assert updated["survival_ended_at"] == ""
    assert updated["survival_last_seconds"] == 3600
    assert updated["survival_total_seconds"] == 3600
    assert updated["survival_recovery_count"] == 1


def test_confirmed_ban_is_deleted_from_local_accounts(tmp_path):
    store = JsonStore(tmp_path)
    saved = account()
    store.write("accounts.json", [saved])
    service = AccountHealthService(store, RegistrationManager(store))

    service._record_result(saved, {
        "updates": {
            "health_status": "banned",
            "health_alive": False,
            "health_checked_at": "2026-07-20T03:00:00+00:00",
            "health_detail": "account_deactivated",
        },
        "recovered": False,
    })

    assert store.read("accounts.json", []) == []
    assert service.status()["banned"] == 1
