from __future__ import annotations

import json
import re

from app.registration.outlook import OutlookMailboxPool, OutlookMailClient
from app.registration.mail import extract_otp


def test_outlook_pool_splits_one_mailbox_into_five_registration_addresses(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    imported = pool.import_text(
        "owner@outlook.com----Password123!----client-id----refresh-token"
    )

    assert imported["available_slots"] == 5

    assigned = []
    for _ in range(5):
        mailbox = pool.acquire(5)
        assigned.append(mailbox["registered_email"])
        mailbox["split_limit"] = 5
        pool.release(mailbox, used=True)

    assert len(set(assigned)) == 5
    assert all(re.fullmatch(r"owner\+[0-9a-f]{12}@outlook\.com", value) for value in assigned)
    assert all("+gpt" not in value for value in assigned)
    summary = pool.summary(5)
    assert summary["available_slots"] == 0
    assert summary["used"] == 1
    stored = json.loads((tmp_path / "outlook_mailboxes.json").read_text(encoding="utf-8"))[0]
    assert set(stored["used_split_aliases"].values()) == {
        value.split("+", 1)[1].split("@", 1)[0] for value in assigned
    }


def test_outlook_client_resolves_a_split_address_back_to_its_base_mailbox(tmp_path):
    path = tmp_path / "outlook_mailboxes.json"
    OutlookMailboxPool(path).import_text(
        "owner@outlook.com----Password123!----client-id----refresh-token"
    )
    client = OutlookMailClient(path, split_limit=5)
    try:
        mailbox = client.create_mailbox()
        assert re.fullmatch(r"owner\+[0-9a-f]{12}@outlook\.com", mailbox["address"])
        assert mailbox["split_alias"] in mailbox["address"]
        resolved = client.existing_mailbox(mailbox["address"])
        assert resolved["base_address"] == "owner@outlook.com"
    finally:
        client.close()

    assert OutlookMailboxPool(path).summary(5)["available_slots"] == 5


def test_outlook_pool_repairs_reversed_client_id_and_refresh_token(tmp_path):
    path = tmp_path / "outlook_mailboxes.json"
    OutlookMailboxPool(path).import_text(
        "owner@outlook.com----Password123!----M.C500_example_refresh_token----"
        "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    )

    mailbox = OutlookMailClient(path).create_mailbox()

    assert mailbox["client_id"] == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    assert mailbox["refresh_token"] == "M.C500_example_refresh_token"


def test_outlook_pool_snapshot_reports_split_and_lease_state(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    pool.import_payload({
        "email": "owner@outlook.com",
        "password": "Password123!",
        "client_id": "client-id",
        "refresh_token": "refresh-token",
    })
    mailbox = pool.acquire(5)

    snapshot = pool.snapshot(5, status="leased")

    assert snapshot["summary"]["leased"] == 1
    assert snapshot["summary"]["available_slots"] == 4
    assert snapshot["items"][0]["status"] == "leased"
    assert snapshot["items"][0]["leased_splits"] == 1
    assert "password" not in snapshot["items"][0]
    assert "refresh_token" not in snapshot["items"][0]
    pool.release(mailbox)


def test_outlook_oauth_retries_common_then_consumers_for_tenant_mismatch():
    client = object.__new__(OutlookMailClient)
    client.request_timeout = 30

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "payload"

        def json(self):
            return self._payload

    calls = []

    class Session:
        @staticmethod
        def request(_method, url, **_kwargs):
            calls.append(url)
            if "/common/" in url:
                return Response(400, {"error": "invalid_grant", "error_description": "AADSTS7000012: different tenant"})
            return Response(200, {"access_token": "graph-token", "expires_in": 3600})

    class Pool:
        @staticmethod
        def mark_failed(*_args):
            raise AssertionError("tenant mismatch must not mark the mailbox failed")

        @staticmethod
        def update_refresh_token(*_args):
            pass

    client.session = Session()
    client.pool = Pool()
    mailbox = {"client_id": "client-id", "refresh_token": "refresh-token"}

    assert client._access_token(mailbox) == "graph-token"
    assert "/common/" in calls[0]
    assert "/consumers/" in calls[1]


def test_outlook_prefers_graph_when_token_has_graph_and_imap_scopes():
    client = object.__new__(OutlookMailClient)
    mailbox = {"access_token_scope": ""}

    def access_token(value, *, refresh=False, scope=None):
        value["access_token_scope"] = (
            "https://graph.microsoft.com/.default "
            "https://graph.microsoft.com/IMAP.AccessAsUser.All"
        )
        return "header.payload.signature"

    client._access_token = access_token
    client._graph_messages = lambda _mailbox, _token: [{"id": "graph-mail"}]
    client._imap_messages = lambda *_args: (_ for _ in ()).throw(AssertionError("Graph must be preferred"))

    assert client._messages(mailbox) == [{"id": "graph-mail"}]


def test_outlook_imap_message_parser_extracts_openai_otp():
    raw = (
        b"From: OpenAI <noreply@example.test>\r\n"
        b"To: owner+a91f5c20d84e@outlook.com\r\n"
        b"Subject: Your verification code is 483921\r\n"
        b"Date: Thu, 30 Jul 2026 10:00:00 +0800\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Your verification code is 483921.\r\n"
    )

    message = OutlookMailClient._parsed_imap_message(b"1", raw)

    assert extract_otp(message) == "483921"
    assert message["receivedDateTime"].startswith("2026-07-30T02:00:00")


def test_outlook_uses_imap_for_outlook_scoped_token():
    client = object.__new__(OutlookMailClient)
    mailbox = {"access_token_scope": ""}
    requested_scopes = []

    def access_token(value, *, refresh=False, scope=None):
        requested_scopes.append(scope)
        if scope is None:
            value["outlook_access_token_scope"] = "https://outlook.office.com/IMAP.AccessAsUser.All"
            return "opaque-outlook-token"
        return "graph-token"

    client._access_token = access_token
    client._imap_messages = lambda _mailbox, _token: [{"id": "mail-1"}]
    client._graph_messages = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("Outlook Graph 读取邮件失败: HTTP 401")
    )

    assert client._messages(mailbox) == [{"id": "mail-1"}]
    assert requested_scopes == ["https://graph.microsoft.com/.default", None]


def test_outlook_marks_disconnected_imap_mailbox_failed():
    client = object.__new__(OutlookMailClient)
    mailbox = {"id": "mailbox-id", "access_token_scope": ""}
    marked = []

    class Pool:
        @staticmethod
        def mark_failed(value, error):
            marked.append((value, error))

    client.pool = Pool()
    def access_token(value, *, refresh=False, scope=None):
        if scope is None:
            value["outlook_access_token_scope"] = "https://outlook.office.com/IMAP.AccessAsUser.All"
        return "token"

    client._access_token = access_token
    client._graph_messages = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("Outlook Graph 读取邮件失败: HTTP 401")
    )
    client._imap_messages = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("Outlook IMAP OAuth 读取邮件失败: User is authenticated but not connected.")
    )

    try:
        client._messages(mailbox)
    except RuntimeError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("expected disconnected IMAP failure")

    assert marked and marked[0][0] is mailbox
