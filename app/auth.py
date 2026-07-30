from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.storage import JsonStore


PBKDF2_ITERATIONS = 310_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt_value = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_value.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(rounds),
        )
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


class UserStore:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    def ensure_default(self) -> None:
        username = os.getenv("REG_ADMIN_USERNAME", "admin").strip() or "admin"
        password = os.getenv("REG_ADMIN_PASSWORD", "admin123")

        def initialize(value: Any) -> list[dict[str, str]]:
            users = value if isinstance(value, list) else []
            if users:
                return users
            return [{"username": username, "password_hash": hash_password(password)}]

        self.store.update("users.json", [], initialize)

    def authenticate(self, username: str, password: str) -> bool:
        users = self.store.read("users.json", [])
        if not isinstance(users, list):
            return False
        target = username.strip().lower()
        for user in users:
            if not isinstance(user, dict):
                continue
            if str(user.get("username") or "").strip().lower() != target:
                continue
            return verify_password(password, str(user.get("password_hash") or ""))
        return False

    def change_password(self, username: str, current_password: str, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("新密码至少需要 8 位")
        if not self.authenticate(username, current_password):
            raise ValueError("当前密码不正确")

        def update(users: Any) -> list[dict[str, str]]:
            items = users if isinstance(users, list) else []
            for user in items:
                if str(user.get("username") or "").strip().lower() == username.strip().lower():
                    user["password_hash"] = hash_password(new_password)
                    return items
            raise ValueError("用户不存在")

        self.store.update("users.json", [], update)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class SessionData:
    username: str
    expires_at: int


class SessionSigner:
    def __init__(self, store: JsonStore, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds
        metadata = store.read("app.json", {})
        if not isinstance(metadata, dict):
            metadata = {}
        secret = str(metadata.get("session_secret") or "")
        if len(secret) < 32:
            secret = secrets.token_urlsafe(48)
            metadata["session_secret"] = secret
            store.write("app.json", metadata)
        self.secret = secret.encode("utf-8")

    def issue(self, username: str) -> str:
        payload = {
            "sub": username,
            "exp": int(time.time()) + self.ttl_seconds,
            "nonce": secrets.token_hex(8),
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def verify(self, token: str) -> SessionData | None:
        try:
            body, signature = token.split(".", 1)
            expected = _b64encode(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(_b64decode(body))
            username = str(payload.get("sub") or "").strip()
            expires_at = int(payload.get("exp") or 0)
            if not username or expires_at <= int(time.time()):
                return None
            return SessionData(username=username, expires_at=expires_at)
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

