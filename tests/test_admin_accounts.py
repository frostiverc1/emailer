import json

import pytest

from conftest import load_handler


@pytest.fixture
def h(table):
    return load_handler("admin", table)


def call(h, route, body=None):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = json.dumps(body)
    resp = h.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_create_and_list_accounts(h):
    status, body = call(h, "POST /admin/accounts", {"name": "Acme"})
    assert status == 201
    assert body["account_id"].startswith("acct_")

    status, listed = call(h, "GET /admin/accounts")
    assert status == 200
    assert [(a["account_id"], a["name"]) for a in listed["accounts"]] == [(body["account_id"], "Acme")]


def test_create_account_requires_name(h):
    assert call(h, "POST /admin/accounts", {})[0] == 400


SERVICE = {"name": "Site", "provider_type": "ses", "ses_from_email": "hi@acme.com"}


def test_service_requires_account_id(h):
    status, body = call(h, "POST /admin/services", SERVICE)
    assert (status, body["error"]) == (400, "Missing field: account_id")


def test_service_rejects_unknown_account(h):
    status, body = call(h, "POST /admin/services", {**SERVICE, "account_id": "acct_nope"})
    assert (status, body["error"]) == (404, "Account not found")


def test_service_is_linked_to_account(h, table):
    account_id = call(h, "POST /admin/accounts", {"name": "Acme"})[1]["account_id"]
    status, body = call(h, "POST /admin/services", {**SERVICE, "account_id": account_id})
    assert status == 201

    stored = table.get_item(Key={"PK": f"SVC#{body['service_id']}", "SK": "META"})["Item"]
    assert stored["account_id"] == account_id

    services = call(h, "GET /admin/services")[1]["services"]
    assert [(s["service_id"], s["account_id"]) for s in services] == [(body["service_id"], account_id)]
