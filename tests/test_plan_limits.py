import json

import pytest

from conftest import load_handler


# --- admin: template count is capped per plan ---

@pytest.fixture
def admin(table):
    return load_handler("admin", table)


def call(h, route, body=None, sub="a", params=None):
    method, resource = route.split(" ", 1)
    event = {"httpMethod": method, "resource": resource}
    if sub is not None:
        event["requestContext"] = {"authorizer": {"claims": {"sub": sub}}}
    if body is not None:
        event["body"] = json.dumps(body)
    if params is not None:
        event["pathParameters"] = params
    resp = h.handler(event, None)
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None)


def test_free_plan_is_capped_at_two_templates(admin):
    assert call(admin, "POST /admin/templates", {"template_id": "t1", "subject_tpl": "s", "html_tpl": "h"})[0] == 201
    assert call(admin, "POST /admin/templates", {"template_id": "t2", "subject_tpl": "s", "html_tpl": "h"})[0] == 201
    status, body = call(admin, "POST /admin/templates", {"template_id": "t3", "subject_tpl": "s", "html_tpl": "h"})
    assert (status, body["error"]) == (403, "Template limit reached for your plan (2)")


def test_updating_an_existing_template_does_not_count_against_the_limit(admin):
    call(admin, "POST /admin/templates", {"template_id": "t1", "subject_tpl": "s", "html_tpl": "h"})
    call(admin, "POST /admin/templates", {"template_id": "t2", "subject_tpl": "s", "html_tpl": "h"})
    assert call(admin, "POST /admin/templates", {"template_id": "t1", "subject_tpl": "s2", "html_tpl": "h"})[0] == 201


def test_personal_plan_allows_more_templates(admin, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    for i in range(6):
        assert call(admin, "POST /admin/templates", {"template_id": f"t{i}", "subject_tpl": "s", "html_tpl": "h"})[0] == 201
    assert call(admin, "POST /admin/templates", {"template_id": "t6", "subject_tpl": "s", "html_tpl": "h"})[0] == 403


def test_professional_plan_has_unlimited_templates(admin, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "professional"})
    for i in range(10):
        assert call(admin, "POST /admin/templates", {"template_id": f"t{i}", "subject_tpl": "s", "html_tpl": "h"})[0] == 201


# --- admin: GET/POST /admin/plan (no Stripe checkout yet, so picking a plan takes effect immediately) ---

def test_defaults_to_free_plan(admin):
    status, body = call(admin, "GET /admin/plan")
    assert (status, body["plan"], body["limits"]["requests_per_month"]) == (200, "free", 200)
    assert body["usage"] == {"requests_this_month": 0, "templates_used": 0}
    assert set(body["catalog"]) == {"free", "personal", "professional", "business"}


def test_setting_a_plan_takes_effect_immediately(admin, table):
    status, body = call(admin, "POST /admin/plan", {"plan": "business"})
    assert (status, body["plan"], body["limits"]["requests_per_month"]) == (200, "business", 25000)
    assert call(admin, "GET /admin/plan")[1]["plan"] == "business"


def test_unknown_plan_is_rejected(admin):
    status, body = call(admin, "POST /admin/plan", {"plan": "enterprise"})
    assert status == 400
    assert "plan must be one of" in body["error"]


def test_plan_snapshot_counts_templates(admin, table):
    call(admin, "POST /admin/templates", {"template_id": "t1", "subject_tpl": "s", "html_tpl": "h"})
    body = call(admin, "GET /admin/plan")[1]
    assert body["usage"]["templates_used"] == 1


def test_plan_is_scoped_to_the_caller(admin):
    call(admin, "POST /admin/plan", {"plan": "business"}, sub="a")
    assert call(admin, "GET /admin/plan", sub="b")[1]["plan"] == "free"


# --- validate: per-plan retention (monthly quota is enforced by API Gateway now, not tested here) ---

KEY_ID = "test-key-id"


@pytest.fixture
def v(table, monkeypatch):
    from unittest import mock
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEYID#{KEY_ID}", "SK": "META", "account_id": "acct_a"})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def send(v):
    body = {"from_email": "hi@acme.com", "template_id": "tpl_x", "template_params": {"to_email": "u@example.com"}}
    event = {
        "httpMethod": "POST",
        "resource": "/v1/send",
        "headers": {},
        "body": json.dumps(body),
        "requestContext": {"identity": {"apiKeyId": KEY_ID}},
    }
    resp = v.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_free_plan_email_record_expires_in_7_days(v, table):
    import time
    status, body = send(v)
    assert status == 202
    record = table.get_item(Key={"PK": f"EMAIL#{body['request_id']}", "SK": "META"})["Item"]
    assert abs(record["ttl"] - (int(time.time()) + 7 * 86400)) < 5


def test_personal_plan_email_record_expires_in_30_days(v, table):
    import time
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    status, body = send(v)
    assert status == 202
    record = table.get_item(Key={"PK": f"EMAIL#{body['request_id']}", "SK": "META"})["Item"]
    assert abs(record["ttl"] - (int(time.time()) + 30 * 86400)) < 5
