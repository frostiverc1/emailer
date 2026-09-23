import base64
import hashlib
import hmac
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
apigw = boto3.client("apigateway")

# Public route, no Cognito auth — split into its own Lambda so a bug or abuse here can't touch the
# account-management routes' IAM permissions (dynamodb:Scan, api key creation, etc). Only what a
# subscription-plan change actually needs: read/write the account's plan, and move its API key
# between usage plans. Kept in sync with the same table in src/validate and src/admin handlers.
PLANS = {
    "free":         {"price_usd": 0,  "requests_per_month": 200,   "templates": 2,    "attachment_bytes": 0,                "retention_days": 7},
    "personal":     {"price_usd": 9,  "requests_per_month": 2000,  "templates": 6,    "attachment_bytes": 500 * 1024,       "retention_days": 30},
    "professional": {"price_usd": 15, "requests_per_month": 5000,  "templates": None, "attachment_bytes": 2 * 1024 * 1024,  "retention_days": 30},
    "business":     {"price_usd": 40, "requests_per_month": 25000, "templates": None, "attachment_bytes": 30 * 1024 * 1024, "retention_days": 30},
}

# API Gateway usage plans are named "<this prefix><tier>" (infra/modules/api/main.tf).
USAGE_PLAN_NAME_PREFIX = os.environ.get("USAGE_PLAN_NAME_PREFIX", "")
_usage_plan_ids = {}


def _usage_plan_id(plan):
    if not _usage_plan_ids:
        for up in apigw.get_usage_plans(limit=500).get("items", []):
            name = up.get("name", "")
            if name.startswith(USAGE_PLAN_NAME_PREFIX):
                _usage_plan_ids[name[len(USAGE_PLAN_NAME_PREFIX):]] = up["id"]
    return _usage_plan_ids.get(plan)


def _move_key_to_plan(account_id, new_plan):
    """Moves the account's current API Gateway key onto the new plan's usage plan, if it has a key."""
    current = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    key_id = current and current.get("api_key_id")
    if not key_id:
        return
    new_usage_plan_id = _usage_plan_id(new_plan)
    if not new_usage_plan_id:
        return
    for up in apigw.get_usage_plans(limit=500).get("items", []):
        if up.get("name", "").startswith(USAGE_PLAN_NAME_PREFIX) and up["id"] != new_usage_plan_id:
            try:
                apigw.delete_usage_plan_key(usagePlanId=up["id"], keyId=key_id)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NotFoundException":
                    logger.exception(f"Failed to unlink key from old usage plan | key_id={key_id}")
    try:
        apigw.create_usage_plan_key(usagePlanId=new_usage_plan_id, keyId=key_id, keyType="API_KEY")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConflictException":
            raise


def handler(event, context):
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        return _resp(500, {"error": "Webhook secret not configured"})

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    sig_header = headers.get("stripe-signature")
    if not sig_header:
        return _resp(400, {"error": "Missing signature"})

    raw_body = event.get("body", "")
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        parts = dict(item.split("=") for item in sig_header.split(","))
        timestamp = parts.get("t")
        v1 = parts.get("v1")
        if not timestamp or not v1:
            return _resp(400, {"error": "Invalid signature format"})

        signed_payload = f"{timestamp}.{raw_body}"
        mac = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256)
        expected_sig = mac.hexdigest()
        if not hmac.compare_digest(expected_sig, v1):
            return _resp(400, {"error": "Invalid signature"})
    except Exception:
        return _resp(400, {"error": "Invalid signature"})

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    event_type = body.get("type")
    data_obj = body.get("data", {}).get("object", {})

    if event_type in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        account_id = data_obj.get("metadata", {}).get("account_id")
        if not account_id:
            return _resp(200, {"status": "ignored, missing account_id"})

        status = data_obj.get("status")

        if event_type != "customer.subscription.deleted" and status in ("active", "trialing"):
            items = data_obj.get("items", {}).get("data", [])
            plan = "free"
            if items:
                lookup_key = items[0].get("price", {}).get("lookup_key")
                if lookup_key in PLANS:
                    plan = lookup_key
        else:
            # canceled, unpaid, past_due, or deleted
            plan = "free"

        table.update_item(
            Key={"PK": f"ACCT#{account_id}", "SK": "META"},
            UpdateExpression="SET #p = :plan",
            ExpressionAttributeNames={"#p": "plan"},
            ExpressionAttributeValues={":plan": plan},
        )
        _move_key_to_plan(account_id, plan)

    return _resp(200, {"status": "success"})


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
