import json

import pytest

from conftest import load_handler


@pytest.fixture
def h(table):
    return load_handler("admin", table)


def call(h, route, body=None, sub="a", params=None):
    event = {"routeKey": route}
    if sub is not None:
        # What the Cognito JWT authorizer passes on.
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    if body is not None:
        event["body"] = json.dumps(body)
    if params is not None:
        event["pathParameters"] = params
    resp = h.handler(event, None)
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None)


TEMPLATE = {"template_id": "tpl_x", "subject_tpl": "Hi", "html_tpl": "<p>Hi</p>"}


def test_not_logged_in(h):
    status, body = call(h, "GET /admin/templates", sub=None)
    assert (status, body["error"]) == (401, "Unauthorized")


def test_template_belongs_to_caller(h, table):
    assert call(h, "POST /admin/templates", {**TEMPLATE, "account_id": "acct_b"})[0] == 201
    assert table.get_item(Key={"PK": "ACCT#acct_a", "SK": "TPL#tpl_x"})["Item"]["subject_tpl"] == "Hi"
    assert "Item" not in table.get_item(Key={"PK": "ACCT#acct_b", "SK": "TPL#tpl_x"})


def test_template_round_trip(h):
    call(h, "POST /admin/templates", TEMPLATE)
    status, body = call(h, "GET /admin/templates/{id}", params={"id": "tpl_x"})
    assert (status, body["subject_tpl"]) == (200, "Hi")

    assert call(h, "PUT /admin/templates/{id}", {"subject_tpl": "Hello"}, params={"id": "tpl_x"})[0] == 200
    assert call(h, "GET /admin/templates/{id}", params={"id": "tpl_x"})[1]["subject_tpl"] == "Hello"

    assert call(h, "DELETE /admin/templates/{id}", params={"id": "tpl_x"})[0] == 204
    assert call(h, "GET /admin/templates/{id}", params={"id": "tpl_x"})[0] == 404


def test_list_templates_only_shows_own(h):
    call(h, "POST /admin/templates", TEMPLATE, sub="a")
    call(h, "POST /admin/templates", {**TEMPLATE, "template_id": "tpl_b"}, sub="b")
    templates = call(h, "GET /admin/templates")[1]["templates"]
    assert [t["template_id"] for t in templates] == ["tpl_x"]


@pytest.mark.parametrize("route", ["GET /admin/templates/{id}", "PUT /admin/templates/{id}"])
def test_other_accounts_template_looks_missing(h, table, route):
    call(h, "POST /admin/templates", TEMPLATE, sub="b")

    status, _ = call(h, route, {"subject_tpl": "hacked"}, params={"id": "tpl_x"})
    assert status == 404
    assert table.get_item(Key={"PK": "ACCT#acct_b", "SK": "TPL#tpl_x"})["Item"]["subject_tpl"] == "Hi"


def test_cannot_delete_other_accounts_template(h, table):
    call(h, "POST /admin/templates", TEMPLATE, sub="b")
    call(h, "DELETE /admin/templates/{id}", params={"id": "tpl_x"})
    assert "Item" in table.get_item(Key={"PK": "ACCT#acct_b", "SK": "TPL#tpl_x"})


def test_usage_only_for_own_keys(h, table):
    table.put_item(Item={"PK": "APIKEY#gk_b", "SK": "META", "account_id": "acct_b"})
    assert call(h, "GET /admin/usage/{api_key}", params={"api_key": "gk_b"})[0] == 404
    assert call(h, "GET /admin/usage/{api_key}", params={"api_key": "gk_b"}, sub="b")[0] == 200


def test_email_status_only_for_own_emails(h, table):
    table.put_item(Item={"PK": "EMAIL#r1", "SK": "META", "account_id": "acct_b", "status": "sent"})
    assert call(h, "GET /admin/emails/{request_id}", params={"request_id": "r1"})[0] == 404
    assert call(h, "GET /admin/emails/{request_id}", params={"request_id": "r1"}, sub="b")[0] == 200


def test_create_api_key_belongs_to_caller(h, table):
    status, body = call(h, "POST /admin/keys", {"allowed_origins": ["https://acme.com"], "account_id": "acct_b"})
    assert status == 201
    assert body["api_key"].startswith("gk_")
    stored = table.get_item(Key={"PK": f"APIKEY#{body['api_key']}", "SK": "META"})["Item"]
    assert (stored["account_id"], stored["active"], stored["allowed_origins"]) == ("acct_a", True, {"https://acme.com"})


def test_api_key_without_origins(h, table):
    body = call(h, "POST /admin/keys")[1]
    assert "allowed_origins" not in table.get_item(Key={"PK": f"APIKEY#{body['api_key']}", "SK": "META"})["Item"]


def test_api_key_bad_origins(h):
    assert call(h, "POST /admin/keys", {"allowed_origins": "https://acme.com"})[0] == 400


def key_row(table, api_key):
    return table.get_item(Key={"PK": f"APIKEY#{api_key}", "SK": "META"}).get("Item")


def test_new_key_revokes_the_previous_one(h, table):
    first = call(h, "POST /admin/keys")[1]["api_key"]
    second = call(h, "POST /admin/keys")[1]["api_key"]
    assert first != second
    assert key_row(table, first)["active"] is False
    assert "revoked_at" in key_row(table, first)
    assert key_row(table, second)["active"] is True


def test_rotation_leaves_other_accounts_keys_alone(h, table):
    theirs = call(h, "POST /admin/keys", sub="b")[1]["api_key"]
    call(h, "POST /admin/keys", sub="a")
    call(h, "POST /admin/keys", sub="a")
    assert key_row(table, theirs)["active"] is True


def test_rotation_when_old_key_row_is_gone(h, table):
    first = call(h, "POST /admin/keys")[1]["api_key"]
    table.delete_item(Key={"PK": f"APIKEY#{first}", "SK": "META"})
    status, body = call(h, "POST /admin/keys")
    assert status == 201
    assert key_row(table, first) is None
    assert key_row(table, body["api_key"])["active"] is True


def test_no_key_yet(h):
    assert call(h, "GET /admin/keys") == (200, {"key": None})


def test_current_key_is_returned_masked(h):
    created = call(h, "POST /admin/keys", {"allowed_origins": ["https://acme.com"]})[1]["api_key"]
    status, body = call(h, "GET /admin/keys")
    key = body["key"]
    assert status == 200
    assert key["hint"] == f"gk_*******{created[-4:]}"
    assert (key["active"], key["allowed_origins"]) == (True, ["https://acme.com"])
    assert created not in json.dumps(body)
    assert created[3:-4] not in json.dumps(body)


def test_current_key_follows_rotation(h):
    call(h, "POST /admin/keys")
    second = call(h, "POST /admin/keys")[1]["api_key"]
    assert call(h, "GET /admin/keys")[1]["key"]["hint"].endswith(second[-4:])


def test_current_key_only_for_own_account(h):
    call(h, "POST /admin/keys", sub="b")
    assert call(h, "GET /admin/keys", sub="a")[1] == {"key": None}


def test_revoked_current_key_shows_inactive(h, table):
    api_key = call(h, "POST /admin/keys")[1]["api_key"]
    table.update_item(
        Key={"PK": f"APIKEY#{api_key}", "SK": "META"},
        UpdateExpression="SET active = :off",
        ExpressionAttributeValues={":off": False},
    )
    assert call(h, "GET /admin/keys")[1]["key"]["active"] is False
