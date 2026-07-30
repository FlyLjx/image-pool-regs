from __future__ import annotations

import base64
import json

import pytest

from app.registration.browser import _account_id, _proxy_config


def _token(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def test_browser_proxy_supports_authenticated_http_proxy():
    assert _proxy_config("http://name:secret@127.0.0.1:7890") == {
        "server": "http://127.0.0.1:7890",
        "username": "name",
        "password": "secret",
    }


def test_browser_proxy_rejects_missing_port():
    with pytest.raises(ValueError, match="host:port"):
        _proxy_config("http://127.0.0.1")


def test_browser_account_id_prefers_chatgpt_account_claim():
    token = _token({
        "sub": "user-1",
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
    })
    assert _account_id(token) == "account-1"
