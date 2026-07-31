from __future__ import annotations

import time

from app.registration import mail as mail_module
from app.registration import protocol as protocol_module
from app.registration.mail import YydsMailClient
from app.registration.protocol import (
    CloudflareChallengeError,
    ExistingAccountRouteError,
    ProtocolRegistrar,
    WrongOtpError,
    _authorization_error,
    _authorization_route,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, url="https://auth.openai.com/"):
        self.status_code = status_code
        self.payload = payload or {}
        self.text = text
        self.headers = headers or {}
        self.url = url

    def json(self):
        return self.payload


def registrar_settings():
    return {
        "registration": {"proxy": "http://127.0.0.1:7897"},
        "mail": {"api_key": "AC-TEST"},
        "sentinel": {},
        "flaresolverr": {
            "enabled": True,
            "url": "http://flaresolverr:8191",
            "max_timeout_ms": 60000,
            "pass_proxy": True,
        },
    }


def test_yyds_randomly_selects_configured_domain_or_auto(monkeypatch):
    client = object.__new__(YydsMailClient)
    client.domains = ["team.edu.yccc.me", "auto"]
    client.email_prefix = ""
    payloads = []
    choices = iter(["team.edu.yccc.me", "auto"])
    monkeypatch.setattr(mail_module.random, "choice", lambda _values: next(choices))

    def fake_request(_method, _path, *, payload, **_kwargs):
        payloads.append(dict(payload))
        domain = payload.get("domain") or "auto.example.test"
        return {"address": f"{payload['localPart']}@{domain}", "token": "mail-token"}

    client._request = fake_request
    client.create_mailbox()
    client.create_mailbox()

    assert payloads[0]["domain"] == "team.edu.yccc.me"
    assert "domain" not in payloads[1]


def test_yyds_domains_only_returns_receiving_domains():
    client = object.__new__(YydsMailClient)
    client.api_base = "https://mail.example.test/v1"
    client.api_key = "TEST-UNIQUE-DOMAINS"
    client._request = lambda *_args, **_kwargs: [
        {
            "domain": "healthy.example.test",
            "isPublic": True,
            "isVerified": True,
            "isMxValid": True,
            "dnsRecords": {"receivingReady": True},
        },
        {"domain": "disabled.example.test", "isVerified": False, "isMxValid": True},
        {"domain": "broken.example.test", "isVerified": True, "isMxValid": False},
    ]

    assert client.list_domains(refresh=True) == ["healthy.example.test"]


def test_flaresolverr_container_request_translates_and_forwards_registration_proxy(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return FakeResponse(payload={
            "status": "ok",
            "solution": {
                "cookies": [
                    {"name": "cf_clearance", "value": "solved", "domain": ".openai.com"},
                    {"name": "oai-did", "value": "device-binding", "domain": ".auth.openai.com"},
                ],
                "userAgent": "Chrome Test",
            },
        })

    monkeypatch.setattr(protocol_module.requests, "post", fake_post)
    ProtocolRegistrar._flare_cache.clear()
    ProtocolRegistrar._flare_failures.clear()
    registrar = ProtocolRegistrar(registrar_settings())
    try:
        assert registrar._solve_cloudflare("https://auth.openai.com/example") is True
    finally:
        registrar.close()

    assert calls[0][0] == "http://flaresolverr:8191/v1"
    assert calls[0][1]["proxy"]["url"] == "http://host.docker.internal:7897"


def test_flaresolverr_solution_is_reused_for_same_proxy(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return FakeResponse(payload={
            "status": "ok",
            "solution": {
                "cookies": [
                    {"name": "cf_clearance", "value": "solved", "domain": ".openai.com"},
                    {"name": "oai-did", "value": "device-binding", "domain": ".auth.openai.com"},
                ],
                "userAgent": "Chrome Test",
            },
        })

    monkeypatch.setattr(protocol_module.requests, "post", fake_post)
    ProtocolRegistrar._flare_cache.clear()
    ProtocolRegistrar._flare_failures.clear()
    first = ProtocolRegistrar(registrar_settings())
    second = ProtocolRegistrar(registrar_settings())
    try:
        assert first._solve_cloudflare("https://auth.openai.com/first") is True
        assert second._solve_cloudflare("https://auth.openai.com/second") is True
    finally:
        first.close()
        second.close()

    assert len(calls) == 1
    cookie_names = list(second.session.cookies)
    assert "cf_clearance" in cookie_names
    assert "oai-did" in cookie_names


def test_flaresolverr_cache_is_isolated_by_target_host(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return FakeResponse(payload={
            "status": "ok",
            "solution": {
                "cookies": [{"name": "oai-did", "value": "device", "domain": ".chatgpt.com"}],
                "userAgent": "Chrome Test",
            },
        })

    monkeypatch.setattr(protocol_module.requests, "post", fake_post)
    ProtocolRegistrar._flare_cache.clear()
    ProtocolRegistrar._flare_failures.clear()
    registrar = ProtocolRegistrar(registrar_settings())
    try:
        assert registrar._solve_cloudflare("https://auth.openai.com/api/accounts/authorize") is True
        assert registrar._solve_cloudflare("https://chatgpt.com/api/auth/csrf") is True
    finally:
        registrar.close()

    assert len(calls) == 2


def test_normal_authorize_response_does_not_start_flaresolverr():
    registrar = ProtocolRegistrar(registrar_settings())
    calls = []
    registrar._request = lambda *_args, **_kwargs: FakeResponse(headers={"server": "cloudflare"}, text="ok")
    registrar._solve_cloudflare = lambda *_args, **_kwargs: calls.append(True) or True
    try:
        registrar._authorize("test@example.test")
    finally:
        registrar.close()

    assert calls == []


def test_authorize_reports_a_persistent_cloudflare_challenge_without_html():
    registrar = ProtocolRegistrar(registrar_settings())
    challenge = FakeResponse(status_code=403, text="<title>Just a moment...</title>")
    registrar._request = lambda *_args, **_kwargs: challenge
    registrar._solve_cloudflare = lambda *_args, **_kwargs: True
    try:
        try:
            registrar._authorize("test@example.test")
        except CloudflareChallengeError as exc:
            assert "当前出口" in str(exc)
            assert "Just a moment" not in str(exc)
        else:
            raise AssertionError("expected a persistent Cloudflare challenge")
    finally:
        registrar.close()


def test_wrong_otp_response_is_not_retried_with_same_code():
    registrar = ProtocolRegistrar(registrar_settings())
    calls = []
    registrar._request = lambda *_args, **_kwargs: calls.append(True) or FakeResponse(
        status_code=401,
        payload={"error": {"code": "wrong_email_otp_code"}},
        text='{"error":{"code":"wrong_email_otp_code"}}',
    )
    try:
        try:
            registrar._validate_otp("111111")
        except WrongOtpError:
            pass
        else:
            raise AssertionError("expected WrongOtpError")
    finally:
        registrar.close()

    assert len(calls) == 1


def test_account_creation_failed_rebuilds_authorization_once(monkeypatch):
    registrar = ProtocolRegistrar(registrar_settings())
    responses = iter([
        FakeResponse(
            status_code=400,
            payload={"error": {"code": "account_creation_failed"}},
            text='{"error":{"code":"account_creation_failed"}}',
        ),
        FakeResponse(status_code=200),
    ])
    request_calls = []
    authorize_calls = []
    registrar._request = lambda *_args, **_kwargs: request_calls.append(True) or next(responses)
    registrar._authorize = lambda email, *_args, **_kwargs: (
        authorize_calls.append(email) or "https://auth.openai.com/create-account/password"
    )
    registrar._renew_authorization_session = lambda: None
    registrar._add_sentinel_headers = lambda *_args, **_kwargs: None
    monkeypatch.setattr(registrar.stop_event, "wait", lambda _seconds: False)
    try:
        registrar._register_password("one@example.test", "Password123!")
    finally:
        registrar.close()

    assert len(request_calls) == 2
    assert authorize_calls == ["one@example.test"]


def test_invalid_auth_step_renews_transport_before_retry():
    registrar = ProtocolRegistrar(registrar_settings())
    responses = iter([
        FakeResponse(
            status_code=400,
            payload={"error": {"code": "invalid_auth_step"}},
            text='{"error":{"code":"invalid_auth_step"}}',
        ),
        FakeResponse(status_code=200),
    ])
    events = []
    registrar._request = lambda *_args, **_kwargs: next(responses)
    registrar._renew_authorization_session = lambda: events.append("renew")
    registrar._authorize = lambda email, *_args, **_kwargs: (
        events.append(f"authorize:{email}") or "https://auth.openai.com/create-account/password"
    )
    registrar._add_sentinel_headers = lambda *_args, **_kwargs: None
    try:
        registrar._register_password("one@example.test", "Password123!")
    finally:
        registrar.close()

    assert events == ["renew", "authorize:one@example.test"]


def test_invalid_auth_step_does_not_resubmit_when_authorize_routes_to_login():
    registrar = ProtocolRegistrar(registrar_settings())
    calls = []
    registrar._request = lambda *_args, **_kwargs: calls.append(True) or FakeResponse(
        status_code=400,
        payload={"error": {"code": "invalid_auth_step"}},
        text='{"error":{"code":"invalid_auth_step"}}',
    )
    registrar._renew_authorization_session = lambda: None
    registrar._authorize = lambda *_args, **_kwargs: "https://auth.openai.com/log-in/password"
    registrar._add_sentinel_headers = lambda *_args, **_kwargs: None
    try:
        try:
            registrar._register_password("existing@example.test", "Password123!")
        except ExistingAccountRouteError:
            pass
        else:
            raise AssertionError("expected ExistingAccountRouteError")
    finally:
        registrar.close()

    assert len(calls) == 1


def test_authorization_route_and_error_payload_are_classified():
    payload = "eyJraW5kIjogIkF1dGhBcGlGYWlsdXJlIiwgImVycm9yQ29kZSI6ICJyYXRlX2xpbWl0X2V4Y2VlZGVkIn0="

    assert _authorization_route("https://auth.openai.com/create-account/password") == "signup"
    assert _authorization_route("https://auth.openai.com/log-in/password") == "existing"
    assert _authorization_route(f"https://auth.openai.com/error?payload={payload}") == "error"
    assert _authorization_error(f"https://auth.openai.com/error?payload={payload}") == "rate_limit_exceeded"


def test_existing_outlook_account_uses_otp_login_and_saves_no_unknown_password():
    registrar = ProtocolRegistrar(registrar_settings())

    class Mail:
        provider_name = "outlook"

        def __init__(self):
            self.committed = False
            self.closed = False

        @staticmethod
        def create_mailbox():
            return {"address": "owner+gpt1@outlook.com"}

        def commit_mailbox(self, _mailbox):
            self.committed = True

        def close(self):
            self.closed = True

    mail = Mail()
    registrar._mail_client = lambda *_args, **_kwargs: mail
    registrar._authorize = lambda *_args, **_kwargs: "https://auth.openai.com/log-in/password"
    registrar._login_existing_with_otp = lambda *_args, **_kwargs: {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "id_token": "",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    try:
        account = registrar.register()
    finally:
        registrar.close()

    assert account["email"] == "owner+gpt1@outlook.com"
    assert account["password"] == ""
    assert account["registration_mode"] == "existing_otp"
    assert mail.committed is True
    assert mail.closed is True


def test_existing_account_login_continues_when_old_code_scan_fails():
    registrar = ProtocolRegistrar(registrar_settings())

    class Mail:
        @staticmethod
        def existing_codes(_mailbox):
            raise RuntimeError("User is authenticated but not connected")

    logs = []
    registrar.logger = lambda level, message: logs.append((level, message))
    registrar._send_login_otp = lambda: 100.0
    registrar._wait_for_mail_code = lambda *_args, **_kwargs: "123456"
    registrar._validate_otp = lambda _code: FakeResponse(payload={"authorization_code": "auth-code"})
    registrar._exchange_token = lambda code: {"access_token": code}
    try:
        tokens = registrar._login_existing_with_otp(
            "owner+gpt1@outlook.com",
            Mail(),
            {"address": "owner+gpt1@outlook.com"},
            "Test User",
            "2000-01-01",
        )
    finally:
        registrar.close()

    assert tokens == {"access_token": "auth-code"}
    assert any("读取旧验证码失败" in message for _level, message in logs)


def test_existing_outlook_slot_is_consumed_even_when_otp_login_fails():
    registrar = ProtocolRegistrar(registrar_settings())

    class Mail:
        provider_name = "outlook"

        def __init__(self):
            self.committed = False
            self.failed = False
            self.failed_error = ""

        @staticmethod
        def create_mailbox():
            return {"address": "owner+gpt2@outlook.com", "base_address": "owner@outlook.com", "id": "mailbox-id"}

        def commit_mailbox(self, _mailbox):
            self.committed = True

        def fail_mailbox(self, _mailbox, error):
            self.failed = True
            self.failed_error = error

        @staticmethod
        def close():
            pass

    mail = Mail()
    registrar._mail_client = lambda *_args, **_kwargs: mail
    registrar._authorize = lambda *_args, **_kwargs: "https://auth.openai.com/log-in/password"
    registrar._login_existing_with_otp = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("registration_disallowed")
    )
    try:
        try:
            registrar.register()
        except RuntimeError as exc:
            assert "registration_disallowed" in str(exc)
        else:
            raise AssertionError("expected existing account login failure")
    finally:
        registrar.close()

    assert mail.committed is True
    assert mail.failed is True
    assert "registration_disallowed" in mail.failed_error


def test_transient_outlook_registration_error_does_not_disable_mailbox():
    registrar = ProtocolRegistrar(registrar_settings())

    class Mail:
        provider_name = "outlook"

        def __init__(self):
            self.failed = False
            self.closed = False

        @staticmethod
        def create_mailbox():
            return {"address": "owner@outlook.com", "base_address": "owner@outlook.com", "id": "mailbox-id"}

        def fail_mailbox(self, _mailbox, _error):
            self.failed = True

        def close(self):
            self.closed = True

    mail = Mail()
    registrar._mail_client = lambda *_args, **_kwargs: mail
    registrar._authorize = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("FlareSolverr 兜底失败: Timeout after 60.0 seconds")
    )
    try:
        try:
            registrar.register()
        except RuntimeError as exc:
            assert "FlareSolverr" in str(exc)
        else:
            raise AssertionError("expected transient registration failure")
    finally:
        registrar.close()

    assert mail.failed is False
    assert mail.closed is True


def test_otp_send_is_globally_paced():
    registrar = ProtocolRegistrar(registrar_settings())
    registrar._request = lambda *_args, **_kwargs: FakeResponse(status_code=200)
    original_interval = ProtocolRegistrar._otp_send_interval
    try:
        ProtocolRegistrar._otp_send_interval = 0.04
        ProtocolRegistrar._otp_last_sent_at = time.monotonic()
        started = time.monotonic()
        registrar._send_otp()
        elapsed = time.monotonic() - started
    finally:
        ProtocolRegistrar._otp_send_interval = original_interval
        ProtocolRegistrar._otp_last_sent_at = 0.0
        registrar.close()

    assert elapsed >= 0.03


def test_otp_send_retries_after_cloudflare_challenge(monkeypatch):
    registrar = ProtocolRegistrar(registrar_settings())
    responses = iter([
        FakeResponse(status_code=403, text="<title>Just a moment...</title>"),
        FakeResponse(status_code=200),
    ])
    logs = []
    registrar.logger = lambda level, message: logs.append((level, message))
    registrar._request = lambda *_args, **_kwargs: next(responses)
    monkeypatch.setattr(registrar, "_solve_cloudflare", lambda *_args, **_kwargs: True)
    try:
        registrar._send_otp()
    finally:
        registrar.close()

    assert any("重新发送邮箱验证码" in message for _level, message in logs)
    assert any("接口响应正常" in message for _level, message in logs)


def test_refresh_token_grant_uses_platform_oauth_endpoint():
    registrar = ProtocolRegistrar(registrar_settings())
    captured = {}

    def request(method, url, **kwargs):
        captured.update({"method": method, "url": url, "json": kwargs.get("json")})
        return FakeResponse(
            status_code=200,
            payload={"access_token": "new-access", "refresh_token": "new-refresh"},
        )

    registrar._request = request
    try:
        result = registrar.refresh("old-refresh")
    finally:
        registrar.close()

    assert result["access_token"] == "new-access"
    assert captured["url"] == "https://auth.openai.com/api/accounts/oauth/token"
    assert captured["json"] == {
        "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh",
    }
