from __future__ import annotations

from app.registration import mail as mail_module
from app.registration.mail import YydsMailClient, extract_otp
from app.registration.protocol import (
    ExistingAccountRouteError,
    ProtocolRegistrar,
    RegistrationDisallowedError,
    _cloudflare_challenge,
    generate_pkce,
    random_password,
)


class FakeResponse:
    def __init__(self, payload, *, status_code=200, url="https://example.test", text=""):
        self.payload = payload
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = {}

    def json(self):
        return self.payload


def test_extract_otp_from_html_message():
    message = {"subject": "Verification code", "html_content": "<strong>Your code is 483921</strong>"}
    assert extract_otp(message) == "483921"


def test_wait_for_code_skips_codes_seen_before_otp_request():
    client = object.__new__(YydsMailClient)
    client._request = lambda *_args, **_kwargs: [
        {"id": "old", "subject": "Your verification code is 111111"},
        {"id": "new", "subject": "Your verification code is 222222"},
    ]

    code = client.wait_for_code(
        {"address": "one@example.test", "token": ""},
        requested_at=0,
        timeout=1,
        interval=0.01,
        excluded_codes={"111111"},
    )

    assert code == "222222"


def test_wait_for_code_reports_mailbox_poll_progress(monkeypatch):
    client = object.__new__(YydsMailClient)
    client._request = lambda *_args, **_kwargs: []
    statuses = []
    ticks = iter((0.0, 0.0, 0.0, 10.1))
    monkeypatch.setattr(mail_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mail_module.time, "sleep", lambda _seconds: None)

    code = client.wait_for_code(
        {"address": "one@example.test", "token": ""},
        requested_at=0,
        timeout=0.01,
        interval=0.01,
        on_status=statuses.append,
    )

    assert code is None
    assert statuses == ["邮箱轮询第 1 次：读取到 0 封邮件，尚未发现新的验证码"]


def test_pkce_and_password_shape():
    verifier, challenge = generate_pkce()
    password = random_password()

    assert len(verifier) >= 43
    assert len(challenge) == 43
    assert any(value.isupper() for value in password)
    assert any(value.islower() for value in password)
    assert any(value.isdigit() for value in password)
    assert any(value in "!@#$%" for value in password)


def test_cloudflare_header_alone_does_not_mark_success_page_as_challenge():
    response = type("Response", (), {"status_code": 200, "headers": {"server": "cloudflare"}, "text": "ok"})()
    blocked = type("Response", (), {"status_code": 403, "headers": {"server": "cloudflare"}, "text": ""})()

    assert _cloudflare_challenge(response) is False
    assert _cloudflare_challenge(blocked) is True


def test_create_account_classifies_registration_disallowed(monkeypatch):
    registrar = ProtocolRegistrar({"registration": {}, "mail": {}, "sentinel": {}, "flaresolverr": {}})
    response = FakeResponse(
        {"error": {"code": "registration_disallowed", "message": "Sorry, we cannot create your account."}},
        status_code=400,
    )
    registrar._request = lambda *_args, **_kwargs: response
    monkeypatch.setattr(registrar, "_add_sentinel_headers", lambda *_args, **_kwargs: None)
    try:
        try:
            registrar._create_account("Test User", "2000-01-01")
        except RegistrationDisallowedError as exc:
            assert "邮箱验证码已通过" in str(exc)
        else:
            raise AssertionError("expected registration disallowed error")
    finally:
        registrar.close()


def test_create_account_classifies_existing_account(monkeypatch):
    registrar = ProtocolRegistrar({"registration": {}, "mail": {}, "sentinel": {}, "flaresolverr": {}})
    response = FakeResponse(
        {"error": {"code": "user_already_exists", "message": "An account already exists for this email address."}},
        status_code=400,
        text='{"error":{"code":"user_already_exists"}}',
    )
    registrar._request = lambda *_args, **_kwargs: response
    monkeypatch.setattr(registrar, "_add_sentinel_headers", lambda *_args, **_kwargs: None)
    try:
        try:
            registrar._create_account("Test User", "2000-01-01")
        except ExistingAccountRouteError as exc:
            assert "验证码登录" in str(exc)
        else:
            raise AssertionError("expected existing account route error")
    finally:
        registrar.close()
