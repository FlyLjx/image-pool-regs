from __future__ import annotations

import json
import re

import pytest

from app.registration import outlook as outlook_module
from app.registration.outlook import OutlookMailboxPool, OutlookMailClient, OutlookOtpPollLimitError
from app.registration.protocol import outlook_error_should_disable
from app.registration.mail import extract_otp


def test_outlook_pool_registers_mother_then_five_split_addresses(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    imported = pool.import_text(
        "owner@outlook.com----Password123!----client-id----refresh-token"
    )

    assert imported["available_slots"] == 6

    assigned = []
    for _ in range(6):
        mailbox = pool.acquire(5)
        assigned.append(mailbox["registered_email"])
        mailbox["split_limit"] = 5
        pool.release(mailbox, used=True)

    assert len(set(assigned)) == 6
    assert assigned[0] == "owner@outlook.com"
    assert all(re.fullmatch(r"owner\+[0-9a-f]{12}@outlook\.com", value) for value in assigned[1:])
    assert all("+gpt" not in value for value in assigned)
    summary = pool.summary(5)
    assert summary["available_slots"] == 0
    assert summary["used"] == 1
    stored = json.loads((tmp_path / "outlook_mailboxes.json").read_text(encoding="utf-8"))[0]
    assert stored["used_split_slots"] == [1, 2, 3, 4, 5]
    assert set(stored["used_split_aliases"].values()) == {
        value.split("+", 1)[1].split("@", 1)[0] for value in assigned[1:]
    }


def test_outlook_pool_exhausts_one_base_mailbox_before_using_the_next(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    pool.import_text(
        "first@outlook.com----Password123!----client-1----refresh-1\n"
        "second@outlook.com----Password123!----client-2----refresh-2"
    )

    assigned = []
    for _ in range(7):
        mailbox = pool.acquire(5)
        assigned.append(mailbox["registered_email"])
        mailbox["split_limit"] = 5
        pool.release(mailbox, used=True)

    assert assigned[0] == "first@outlook.com"
    assert all(value.startswith("first+") for value in assigned[1:6])
    assert assigned[6] == "second@outlook.com"


def test_outlook_pool_skips_leased_base_and_registers_next_base(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    pool.import_text(
        "first@outlook.com----Password123!----client-1----refresh-1\n"
        "second@outlook.com----Password123!----client-2----refresh-2"
    )

    first = pool.acquire(3)

    assert first["registered_email"] == "first@outlook.com"
    second = pool.acquire(3)
    assert second["registered_email"] == "second@outlook.com"
    pool.release(second)
    pool.release(first, used=True)
    split = pool.acquire(3)
    assert split["registered_email"].startswith("first+")
    pool.release(split)


def test_outlook_pool_old_alias_slots_do_not_skip_mother_mailbox(tmp_path):
    path = tmp_path / "outlook_mailboxes.json"
    path.write_text(
        json.dumps([
            {
                "email": "owner@outlook.com",
                "password": "Password123!",
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "status": "available",
                "used_split_slots": [1, 2],
                "imported_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-02T00:00:00+00:00",
            }
        ]),
        encoding="utf-8",
    )
    pool = OutlookMailboxPool(path)

    mailbox = pool.acquire(5)

    assert mailbox["registered_email"] == "owner@outlook.com"
    assert mailbox["split_index"] == 0
    pool.release(mailbox, used=True)
    next_mailbox = pool.acquire(5)
    assert next_mailbox["registered_email"].startswith("owner+")
    assert next_mailbox["split_index"] == 3
    pool.release(next_mailbox)


def test_outlook_client_resolves_a_split_address_back_to_its_base_mailbox(tmp_path):
    path = tmp_path / "outlook_mailboxes.json"
    OutlookMailboxPool(path).import_text(
        "owner@outlook.com----Password123!----client-id----refresh-token"
    )
    client = OutlookMailClient(path, split_limit=5)
    try:
        mailbox = client.create_mailbox()
        assert mailbox["address"] == "owner@outlook.com"
        assert mailbox["split_alias"] == ""
        resolved = client.existing_mailbox(mailbox["address"])
        assert resolved["base_address"] == "owner@outlook.com"
    finally:
        client.close()

    assert OutlookMailboxPool(path).summary(5)["available_slots"] == 6


def test_outlook_client_marks_uncommitted_mailbox_failed(tmp_path):
    path = tmp_path / "outlook_mailboxes.json"
    OutlookMailboxPool(path).import_text(
        "owner@outlook.com----Password123!----client-id----refresh-token"
    )
    client = OutlookMailClient(path, split_limit=5)
    mailbox = client.create_mailbox()

    client.fail_mailbox(mailbox, "split failed")
    client.close()

    snapshot = OutlookMailboxPool(path).snapshot(5, status="failed")
    assert snapshot["summary"]["failed"] == 1
    assert snapshot["summary"]["available_slots"] == 0
    assert snapshot["items"][0]["email"] == "owner@outlook.com"
    assert "split failed" in snapshot["items"][0]["last_error"]


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
    assert snapshot["summary"]["available_slots"] == 5
    assert snapshot["items"][0]["status"] == "leased"
    assert snapshot["items"][0]["leased_splits"] == 1
    assert snapshot["items"][0]["base_leased"] is True
    assert "password" not in snapshot["items"][0]
    assert "refresh_token" not in snapshot["items"][0]
    pool.release(mailbox)


def test_outlook_pool_deletes_selected_mailboxes_and_can_clear_all(tmp_path):
    pool = OutlookMailboxPool(tmp_path / "outlook_mailboxes.json")
    for index in range(3):
        pool.import_payload({
            "email": f"owner{index}@outlook.com",
            "password": "Password123!",
            "client_id": f"client-{index}",
            "refresh_token": f"refresh-{index}",
        })
    snapshot = pool.snapshot(5, page_size=20)
    selected_id = snapshot["items"][0]["id"]

    deleted = pool.delete([selected_id])
    assert deleted["removed"] == 1
    assert deleted["total"] == 2
    assert selected_id not in {item["id"] for item in pool.snapshot(5)["items"]}

    cleared = pool.delete(clear_all=True)
    assert cleared["removed"] == 2
    assert cleared["total"] == 0
    assert pool.snapshot(5)["items"] == []


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


def test_outlook_wait_for_code_switches_mailbox_after_poll_limit(monkeypatch):
    client = object.__new__(OutlookMailClient)
    calls = []
    statuses = []

    def messages(_mailbox):
        calls.append(1)
        return [{"id": f"mail-{len(calls)}", "subject": "hello", "receivedDateTime": ""}]

    client._messages = messages
    monkeypatch.setattr(outlook_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(OutlookOtpPollLimitError, match="3 次仍未收到新验证码") as caught:
        client.wait_for_code(
            {"address": "owner@outlook.com"},
            requested_at=0,
            timeout=120,
            interval=0.01,
            on_status=statuses.append,
            max_polls=3,
        )

    assert len(calls) == 3
    assert statuses[-1] == "Outlook 邮箱轮询已达 3 次仍未收到新验证码，当前母号标记失效并切换下一个号"
    assert outlook_error_should_disable(caught.value) is True


def test_outlook_generic_provider_errors_release_mailbox_for_retry():
    assert outlook_error_should_disable("Outlook Graph 读取邮件失败: HTTP 401") is False
    assert outlook_error_should_disable("提交注册密码失败: HTTP 500") is False
    assert outlook_error_should_disable("AADSTS70000: User account is found to be in service abuse mode") is True
