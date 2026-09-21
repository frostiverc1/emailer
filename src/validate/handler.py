import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ops_table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
sqs = boto3.client("sqs")
QUEUE_URL = os.environ["QUEUE_URL"]

# Fixed width so the worker's lease check can compare timestamps as strings.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
EMAIL_RECORD_TTL_SECONDS = 30 * 86400
# A bare address only. Display names, angle brackets and lists are rejected so the domain check can't be sidestepped.
FROM_EMAIL = re.compile(r"[^@\s<>,;\"]+@([A-Za-z0-9.-]+)")


def handler(event, context):
    # --- parse input ---
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    api_key = headers.get("x-api-key")
    origin = headers.get("origin", "")
    if not api_key:
        return _resp(401, {"error": "Missing API key"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    for field in ("from_email", "template_id", "template_params"):
        if not body.get(field):
            return _resp(400, {"error": f"Missing field: {field}"})

    to_email = body["template_params"].get("to_email")
    if not to_email:
        return _resp(400, {"error": "template_params.to_email is required"})

    # --- 1. look up API key (ops table) ---
    key_record = ops_table.get_item(
        Key={"PK": f"APIKEY#{api_key}", "SK": "META"},
        ProjectionExpression="account_id, allowed_origins, daily_limit, active",
    )
    item = key_record.get("Item")
    if not item or not item.get("active", False) or not item.get("account_id"):
        return _resp(401, {"error": "Invalid API key"})

    # --- 2. origin check ---
    allowed = item.get("allowed_origins", set())
    if allowed and origin and origin not in allowed:
        return _resp(403, {"error": "Origin not allowed"})

    # --- 3. from_email must be on a verified domain owned by the key's account ---
    sender_error = _sender_error(body["from_email"], item["account_id"])
    if sender_error:
        return _resp(403, {"error": sender_error})

    # --- 4. daily quota (atomic increment, check after, ops table) ---
    today = date.today().isoformat()
    usage = ops_table.update_item(
        Key={"PK": f"USAGE#{api_key}", "SK": f"DAY#{today}"},
        UpdateExpression="ADD #c :inc SET #ttl = if_not_exists(#ttl, :ttl_val)",
        ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":inc": 1,
            ":ttl_val": int(time.time()) + 30 * 86400,
        },
        ReturnValues="UPDATED_NEW",
    )
    if int(usage["Attributes"]["count"]) > int(item.get("daily_limit", 500)):
        return _resp(429, {"error": "Daily quota exceeded"})

    # --- 5. email record: status log, and the worker's idempotency gate ---
    # Written before the SQS message so the worker never sees a message without a record.
    request_id = str(uuid.uuid4())
    email_key = {"PK": f"EMAIL#{request_id}", "SK": "META"}
    created_at = datetime.now(timezone.utc).strftime(TS_FORMAT)
    expires_at = int(time.time()) + EMAIL_RECORD_TTL_SECONDS
    ops_table.put_item(
        Item={
            **email_key,
            "status": "queued",
            "account_id": item["account_id"],
            "template_id": body["template_id"],
            "to_email": to_email,
            "attempts": 0,
            "created_at": created_at,
            "ttl": expires_at,
        },
        ConditionExpression="attribute_not_exists(PK)",
    )
    _index_email(item["account_id"], request_id, created_at, expires_at)

    # --- 6. queue the job ---
    try:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                "request_id": request_id,
                "api_key": api_key,
                "account_id": item["account_id"],
                "from_email": body["from_email"],
                "template_id": body["template_id"],
                "template_params": body["template_params"],
                "queued_at": datetime.utcnow().isoformat(),
            }),
        )
    except Exception as exc:
        logger.exception(f"Failed to queue email | request_id={request_id}")
        _mark_queue_failed(email_key, exc)
        return _resp(500, {"error": "Failed to queue email"})

    return _resp(202, {"status": "queued", "request_id": request_id})


def _sender_error(from_email, account_id):
    match = FROM_EMAIL.fullmatch(from_email) if isinstance(from_email, str) else None
    if not match:
        return "from_email must be a plain email address, e.g. hello@yourdomain.com"

    domain = match.group(1).lower()
    record = ops_table.get_item(
        Key={"PK": f"DOMAIN#{domain}", "SK": "META"},
        ProjectionExpression="account_id, #s",
        ExpressionAttributeNames={"#s": "status"},
    ).get("Item")
    # Same message for "not added", "not verified yet" and "someone else's", so accounts can't probe each other.
    if not record or record.get("account_id") != account_id or record.get("status") != "verified":
        return f"{domain} is not a verified domain on this account"
    return None


def _index_email(account_id, request_id, created_at, expires_at):
    """Lets the admin API list an account's emails newest first, without a scan or a GSI.

    Best effort: the list is a convenience, so a failure here must never stop the email from being sent.
    The row is a pointer only. The status lives on the EMAIL# record, which the worker updates.
    """
    try:
        ops_table.put_item(Item={
            "PK": f"ACCT#{account_id}",
            # created_at is fixed-width UTC, so sorting by SK sorts by time.
            "SK": f"EMAIL#{created_at}#{request_id}",
            "request_id": request_id,
            "ttl": expires_at,
        })
    except Exception:
        logger.exception(f"Failed to index email for listing | request_id={request_id}")


def _mark_queue_failed(email_key, exc):
    # Best effort: if this also fails, the record stays "queued" and is never picked up.
    try:
        ops_table.update_item(
            Key=email_key,
            UpdateExpression="SET #s = :failed, failed_at = :now, error_code = :code, error_message = :msg",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": "failed",
                ":now": datetime.now(timezone.utc).strftime(TS_FORMAT),
                ":code": "QUEUE_ERROR",
                ":msg": str(exc)[:1000],
            },
        )
    except Exception:
        logger.exception(f"Failed to mark email QUEUE_ERROR | {email_key['PK']}")


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
