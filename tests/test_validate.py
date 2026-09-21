import json
from unittest import mock

import pytest

from conftest import load_handler

KEY = "gk_test"


@pytest.fixture
def h(table, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEY#{KEY}", "SK": "META", "account_id": "acct_a", "active": True})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def send(h, **overrides):
    body = {"from_email": "hi@acme.com", "template_id": "tpl_x", "template_params": {"to_email": "u@example.com"}}
    body.update(overrides)
    resp = h.handler({"headers": {"x-api-key": KEY}, "body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_send_from_own_verified_domain(h):
    status, body = send(h)
    assert (status, body["status"]) == (202, "queued")
    queued = json.loads(h.sqs.send_message.call_args.kwargs["MessageBody"])
    assert (queued["account_id"], queued["from_email"]) == ("acct_a", "hi@acme.com")


def test_from_domain_is_case_insensitive(h):
    assert send(h, from_email="Hi@ACME.com")[0] == 202


def test_from_email_required(h):
    status, body = send(h, from_email="")
    assert (status, body["error"]) == (400, "Missing field: from_email")


@pytest.mark.parametrize("from_email", [
    "not-an-email",
    "@acme.com",
    "Acme <hi@acme.com>",
    "hi@acme.com, ceo@bank.com",
    "hi@evil.com@acme.com",
    "hi@acme.com ",
])
def test_from_email_must_be_a_plain_address(h, from_email):
    assert send(h, from_email=from_email)[0] == 403
    h.sqs.send_message.assert_not_called()


def test_domain_not_added(h):
    status, body = send(h, from_email="hi@other.com")
    assert (status, body["error"]) == (403, "other.com is not a verified domain on this account")


def test_domain_not_verified_yet(h, table):
    table.put_item(Item={"PK": "DOMAIN#new.com", "SK": "META", "account_id": "acct_a", "status": "pending"})
    assert send(h, from_email="hi@new.com")[0] == 403


def test_domain_owned_by_another_account(h, table):
    table.put_item(Item={"PK": "DOMAIN#theirs.com", "SK": "META", "account_id": "acct_b", "status": "verified"})
    assert send(h, from_email="hi@theirs.com")[0] == 403
    h.sqs.send_message.assert_not_called()


def test_key_without_account_is_rejected(h, table):
    table.put_item(Item={"PK": "APIKEY#gk_old", "SK": "META", "service_id": "svc_old", "active": True})
    resp = h.handler({"headers": {"x-api-key": "gk_old"}, "body": json.dumps({
        "from_email": "hi@acme.com", "template_id": "t", "template_params": {"to_email": "u@example.com"},
    })}, None)
    assert resp["statusCode"] == 401


def test_revoked_key_is_rejected(h, table):
    table.update_item(
        Key={"PK": f"APIKEY#{KEY}", "SK": "META"},
        UpdateExpression="SET active = :off",
        ExpressionAttributeValues={":off": False},
    )
    assert send(h)[0] == 401
    h.sqs.send_message.assert_not_called()
