import json
from unittest import mock

import pytest

from conftest import load_handler

KEY_ID = "test-key-id"


@pytest.fixture
def h(table, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEYID#{KEY_ID}", "SK": "META", "account_id": "acct_a"})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def send(h, api_key_id=KEY_ID, overrides=None):
    body = {"from": "hi@acme.com", "to": "u@example.com", "template_id": "tpl_x", "template_params": {}}
    body.update(overrides or {})
    event = {
        "httpMethod": "POST",
        "resource": "/v1/send",
        "headers": {},
        "body": json.dumps(body),
        "requestContext": {"identity": {"apiKeyId": api_key_id} if api_key_id else {}},
    }
    resp = h.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_send_from_own_verified_domain(h):
    status, body = send(h)
    assert (status, body["status"]) == (202, "queued")
    queued = json.loads(h.sqs.send_message.call_args.kwargs["MessageBody"])
    assert (queued["account_id"], queued["from"]) == ("acct_a", "hi@acme.com")


def test_from_domain_is_case_insensitive(h):
    assert send(h, overrides={"from": "Hi@ACME.com"})[0] == 202


def test_from_required(h):
    status, body = send(h, overrides={"from": ""})
    assert (status, body["error"]) == (400, "Missing field: from")


@pytest.mark.parametrize("bad_from", [
    "not-an-email",
    "@acme.com",
    "Acme <hi@acme.com>",
    "hi@acme.com, ceo@bank.com",
    "hi@evil.com@acme.com",
    "hi@acme.com ",
])
def test_from_must_be_a_plain_address(h, bad_from):
    assert send(h, overrides={"from": bad_from})[0] == 403
    h.sqs.send_message.assert_not_called()


def test_domain_not_added(h):
    status, body = send(h, overrides={"from": "hi@other.com"})
    assert (status, body["error"]) == (403, "other.com is not a verified domain on this account")


def test_domain_not_verified_yet(h, table):
    table.put_item(Item={"PK": "DOMAIN#new.com", "SK": "META", "account_id": "acct_a", "status": "pending"})
    assert send(h, overrides={"from": "hi@new.com"})[0] == 403


def test_domain_owned_by_another_account(h, table):
    table.put_item(Item={"PK": "DOMAIN#theirs.com", "SK": "META", "account_id": "acct_b", "status": "verified"})
    assert send(h, overrides={"from": "hi@theirs.com"})[0] == 403
    h.sqs.send_message.assert_not_called()


def test_to_required(h):
    status, body = send(h, overrides={"to": ""})
    assert (status, body["error"]) == (400, "Missing field: to")


def test_to_does_not_need_to_be_on_a_verified_domain(h):
    # Unlike from, to is just where the email goes, so it can be any address at all.
    assert send(h, overrides={"to": "anyone@somewhere-unrelated.com"})[0] == 202


def test_reply_to_is_optional(h):
    status, body = send(h)
    assert (status, body["status"]) == (202, "queued")
    queued = json.loads(h.sqs.send_message.call_args.kwargs["MessageBody"])
    assert queued["reply_to"] is None


def test_reply_to_is_passed_through_to_the_queue(h):
    status, _ = send(h, overrides={"reply_to": "visitor@example.com"})
    assert status == 202
    queued = json.loads(h.sqs.send_message.call_args.kwargs["MessageBody"])
    assert queued["reply_to"] == "visitor@example.com"


def test_reply_to_does_not_need_a_verified_domain(h):
    # It's the visitor's own address, not something this account sends as.
    assert send(h, overrides={"reply_to": "visitor@somewhere-unrelated.com"})[0] == 202


@pytest.mark.parametrize("bad_reply_to", [
    "not-an-email",
    "@acme.com",
    "Visitor <visitor@example.com>",
    "visitor@example.com, other@example.com",
])
def test_reply_to_must_be_a_plain_address(h, bad_reply_to):
    status, body = send(h, overrides={"reply_to": bad_reply_to})
    assert (status, body["error"]) == (400, "reply_to must be a plain email address, e.g. visitor@example.com")
    h.sqs.send_message.assert_not_called()


def test_missing_api_key_id_is_rejected(h):
    # API Gateway always sets this for a key-required route; a missing one means the event is malformed.
    assert send(h, api_key_id=None)[0] == 401
    h.sqs.send_message.assert_not_called()


def test_unknown_api_key_id_is_rejected(h):
    # The key existed when API Gateway accepted the request but was deleted (e.g. replaced) before
    # this Lambda ran. Rare, but validate should still refuse rather than guess the account.
    assert send(h, api_key_id="deleted-key-id")[0] == 401
    h.sqs.send_message.assert_not_called()
