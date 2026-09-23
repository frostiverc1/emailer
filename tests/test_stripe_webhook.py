import hashlib
import hmac
import json
import time

import pytest

from conftest import load_handler

SECRET = "whsec_test"


@pytest.fixture
def h(table, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    module = load_handler("stripe_webhook", table)
    module.table = table
    return module


def stripe_event(payload_dict, secret=SECRET):
    payload = json.dumps(payload_dict)
    t = int(time.time())
    signed_payload = f"{t}.{payload}"
    mac = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256)
    sig = mac.hexdigest()
    return {
        "httpMethod": "POST",
        "resource": "/v1/stripe-webhook",
        "headers": {"stripe-signature": f"t={t},v1={sig}"},
        "body": payload,
    }


def subscription_event(event_type="customer.subscription.updated", status="active", lookup_key="professional", account_id="acct_a"):
    return stripe_event({
        "type": event_type,
        "data": {
            "object": {
                "metadata": {"account_id": account_id} if account_id else {},
                "status": status,
                "items": {"data": [{"price": {"lookup_key": lookup_key}}]},
            }
        },
    })


def test_updates_the_account_plan(h, table):
    resp = h.handler(subscription_event(), None)
    assert resp["statusCode"] == 200
    item = table.get_item(Key={"PK": "ACCT#acct_a", "SK": "META"}).get("Item")
    assert item["plan"] == "professional"


def test_cancelled_subscription_drops_to_free(h, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "business"})
    resp = h.handler(subscription_event(event_type="customer.subscription.deleted", status="canceled"), None)
    assert resp["statusCode"] == 200
    assert table.get_item(Key={"PK": "ACCT#acct_a", "SK": "META"})["Item"]["plan"] == "free"


def test_missing_account_id_is_ignored_not_errored(h, table):
    resp = h.handler(subscription_event(account_id=None), None)
    assert resp["statusCode"] == 200
    assert "Item" not in table.get_item(Key={"PK": "ACCT#acct_a", "SK": "META"})


def test_invalid_signature_is_rejected(h):
    event = {
        "httpMethod": "POST",
        "resource": "/v1/stripe-webhook",
        "headers": {"stripe-signature": "t=1,v1=bad"},
        "body": "{}",
    }
    assert h.handler(event, None)["statusCode"] == 400


def test_missing_signature_is_rejected(h):
    event = {"httpMethod": "POST", "resource": "/v1/stripe-webhook", "headers": {}, "body": "{}"}
    assert h.handler(event, None)["statusCode"] == 400


def test_missing_secret_configured_fails_closed(table, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    module = load_handler("stripe_webhook", table)
    module.table = table
    assert module.handler(subscription_event(), None)["statusCode"] == 500


def test_moves_the_account_key_to_the_new_plans_usage_plan(h, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "CURRENT_KEY", "api_key_id": "key-1"})
    key = h.apigw.create_api_key(name="k1", enabled=True)
    old_plan = h.apigw.create_usage_plan(name="emailer-test-free", quota={"limit": 200, "period": "MONTH"}, throttle={"rateLimit": 2.0, "burstLimit": 5})
    new_plan = h.apigw.create_usage_plan(name="emailer-test-professional", quota={"limit": 5000, "period": "MONTH"}, throttle={"rateLimit": 10.0, "burstLimit": 20})
    h.apigw.create_usage_plan_key(usagePlanId=old_plan["id"], keyId=key["id"], keyType="API_KEY")
    table.update_item(
        Key={"PK": "ACCT#acct_a", "SK": "CURRENT_KEY"},
        UpdateExpression="SET api_key_id = :id",
        ExpressionAttributeValues={":id": key["id"]},
    )

    h.handler(subscription_event(lookup_key="professional"), None)

    old_keys = [k["id"] for k in h.apigw.get_usage_plan_keys(usagePlanId=old_plan["id"])["items"]]
    new_keys = [k["id"] for k in h.apigw.get_usage_plan_keys(usagePlanId=new_plan["id"])["items"]]
    assert key["id"] not in old_keys
    assert key["id"] in new_keys
