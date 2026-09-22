import json

import pytest

from conftest import load_handler


# --- admin: template count is capped per plan ---

@pytest.fixture
def admin(table):
    return load_handler("admin", table)


def call(h, route, body=None, sub="a", params=None):
    event = {"routeKey": route, "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}}}
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


# --- admin: GET/POST /admin/plan (no Stripe yet, so upgrading takes effect immediately) ---

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


def test_plan_snapshot_counts_templates_and_this_months_usage(admin, table):
    call(admin, "POST /admin/templates", {"template_id": "t1", "subject_tpl": "s", "html_tpl": "h"})
    import datetime
    month = datetime.date.today().strftime("%Y-%m")
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "CURRENT_KEY", "api_key": "gk_test"})
    table.put_item(Item={"PK": "USAGE#gk_test", "SK": f"MONTH#{month}", "count": 12})
    body = call(admin, "GET /admin/plan")[1]
    assert body["usage"] == {"requests_this_month": 12, "templates_used": 1}


def test_plan_is_scoped_to_the_caller(admin):
    call(admin, "POST /admin/plan", {"plan": "business"}, sub="a")
    assert call(admin, "GET /admin/plan", sub="b")[1]["plan"] == "free"

# --- stripe webhooks ---

def test_stripe_webhook_updates_plan(admin, table, monkeypatch):
    import hmac
    import hashlib
    import time
    
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    
    payload = json.dumps({
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "metadata": {"account_id": "acct_a"},
                "status": "active",
                "items": {"data": [{"price": {"lookup_key": "professional"}}]}
            }
        }
    })
    
    t = int(time.time())
    signed_payload = f"{t}.{payload}"
    mac = hmac.new(b"whsec_test", signed_payload.encode('utf-8'), hashlib.sha256)
    sig = mac.hexdigest()
    
    event = {
        "routeKey": "POST /v1/stripe-webhook",
        "headers": {"stripe-signature": f"t={t},v1={sig}"},
        "body": payload
    }
    
    resp = admin.handler(event, None)
    assert resp["statusCode"] == 200
    
    item = table.get_item(Key={"PK": "ACCT#acct_a", "SK": "META"}).get("Item")
    assert item["plan"] == "professional"

def test_stripe_webhook_invalid_sig(admin, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    event = {
        "routeKey": "POST /v1/stripe-webhook",
        "headers": {"stripe-signature": "t=1,v1=bad"},
        "body": "{}"
    }
    resp = admin.handler(event, None)
    assert resp["statusCode"] == 400

# --- validate: monthly quota and per-plan retention ---

KEY = "gk_test"


@pytest.fixture
def v(table, monkeypatch):
    from unittest import mock
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEY#{KEY}", "SK": "META", "account_id": "acct_a", "active": True})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def send(v):
    body = {"from_email": "hi@acme.com", "template_id": "tpl_x", "template_params": {"to_email": "u@example.com"}}
    resp = v.handler({"headers": {"x-api-key": KEY}, "body": json.dumps(body)}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_free_plan_blocks_after_200_requests_this_month(v, table):
    import datetime
    month = datetime.date.today().strftime("%Y-%m")
    table.put_item(Item={"PK": f"USAGE#{KEY}", "SK": f"MONTH#{month}", "count": 200})
    status, body = send(v)
    assert (status, body["error"]) == (429, "Monthly quota exceeded")
    v.sqs.send_message.assert_not_called()


def test_personal_plan_gets_a_higher_monthly_quota(v, table):
    import datetime
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    month = datetime.date.today().strftime("%Y-%m")
    table.put_item(Item={"PK": f"USAGE#{KEY}", "SK": f"MONTH#{month}", "count": 200})
    assert send(v)[0] == 202


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
