import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ops_table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
sqs = boto3.client("sqs")
s3 = boto3.client("s3")
QUEUE_URL = os.environ["QUEUE_URL"]
ATTACHMENTS_BUCKET = os.environ["ATTACHMENTS_BUCKET_NAME"]

# Fixed width so the worker's lease check can compare timestamps as strings.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# A bare address only. Display names, angle brackets and lists are rejected so the domain check can't be sidestepped.
FROM_EMAIL = re.compile(r"[^@\s<>,;\"]+@([A-Za-z0-9.-]+)")
# Keeps an uploaded filename safe to use in an S3 key and, later, a MIME attachment header.
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9 ._-]")
UPLOAD_URL_TTL_SECONDS = 15 * 60

# No billing yet, so every account sits on "free" until a plan is set on its ACCT#/META row.
# Monthly request quota isn't enforced here any more: API Gateway rejects an over-quota key
# before this Lambda is even invoked, per the usage plan matching the account's plan (infra/modules/api).
DEFAULT_PLAN = "free"
PLANS = {
    "free":         {"price_usd": 0,  "requests_per_month": 200,   "templates": 2,    "attachment_bytes": 0,                "retention_days": 7,  "rate_limit": 2,  "burst_limit": 5},
    "personal":     {"price_usd": 9,  "requests_per_month": 2000,  "templates": 6,    "attachment_bytes": 500 * 1024,       "retention_days": 30, "rate_limit": 5,  "burst_limit": 10},
    "professional": {"price_usd": 15, "requests_per_month": 5000,  "templates": None, "attachment_bytes": 2 * 1024 * 1024,  "retention_days": 30, "rate_limit": 10, "burst_limit": 20},
    "business":     {"price_usd": 40, "requests_per_month": 25000, "templates": None, "attachment_bytes": 25 * 1024 * 1024, "retention_days": 30, "rate_limit": 25, "burst_limit": 50},
}


def handler(event, context):
    route = f"{event.get('httpMethod')} {event.get('resource')}"
    if route == "POST /v1/attachments/upload-url":
        return _upload_url(event)
    return _send(event)


def _caller(event):
    """The API key's id (from API Gateway, which already validated the key and its quota) mapped
    to the account it belongs to. Returns (account_id, allowed_origins) or None if unresolvable —
    which should only happen for a key that was deleted after the request was already accepted."""
    api_key_id = event.get("requestContext", {}).get("identity", {}).get("apiKeyId")
    if not api_key_id:
        return None
    item = ops_table.get_item(
        Key={"PK": f"APIKEYID#{api_key_id}", "SK": "META"},
        ProjectionExpression="account_id, allowed_origins",
    ).get("Item")
    if not item or not item.get("account_id"):
        return None
    return item["account_id"], item.get("allowed_origins", set())


def _send(event):
    # --- parse input ---
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = headers.get("origin", "")

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

    reply_to = body.get("reply_to")
    if reply_to and not FROM_EMAIL.fullmatch(reply_to):
        return _resp(400, {"error": "reply_to must be a plain email address, e.g. visitor@example.com"})

    # --- 1. resolve the API key API Gateway already validated ---
    caller = _caller(event)
    if not caller:
        return _resp(401, {"error": "Invalid API key"})
    account_id, allowed_origins = caller
    api_key_id = event["requestContext"]["identity"]["apiKeyId"]

    # --- 2. origin check ---
    if allowed_origins and origin and origin not in allowed_origins:
        return _resp(403, {"error": "Origin not allowed"})

    # --- 3. from_email must be on a verified domain owned by the key's account ---
    sender_error = _sender_error(body["from_email"], account_id)
    if sender_error:
        return _resp(403, {"error": sender_error})

    # --- 4. attachments, if any, must belong to this account and fit the plan's total ---
    limits = _plan_limits(account_id)
    attachments, attachment_error = _validate_attachments(body.get("attachments"), account_id, limits)
    if attachment_error:
        return _resp(403, {"error": attachment_error})

    # --- 5. email record: status log, and the worker's idempotency gate ---
    # Written before the SQS message so the worker never sees a message without a record.
    request_id = str(uuid.uuid4())
    email_key = {"PK": f"EMAIL#{request_id}", "SK": "META"}
    created_at = datetime.now(timezone.utc).strftime(TS_FORMAT)
    expires_at = int(datetime.now(timezone.utc).timestamp()) + limits["retention_days"] * 86400
    ops_table.put_item(
        Item={
            **email_key,
            "status": "queued",
            "account_id": account_id,
            "template_id": body["template_id"],
            "to_email": to_email,
            "attempts": 0,
            "created_at": created_at,
            "ttl": expires_at,
        },
        ConditionExpression="attribute_not_exists(PK)",
    )
    _index_email(account_id, request_id, created_at, expires_at)

    # --- 6. queue the job ---
    try:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                "request_id": request_id,
                "api_key_id": api_key_id,
                "account_id": account_id,
                "from_email": body["from_email"],
                "reply_to": reply_to,
                "template_id": body["template_id"],
                "template_params": body["template_params"],
                "attachments": attachments,
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


def _upload_url(event):
    """Issues a presigned S3 POST for one attachment, capped by the caller's plan."""
    caller = _caller(event)
    if not caller:
        return _resp(401, {"error": "Invalid API key"})
    account_id, _allowed_origins = caller

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    filename = body.get("filename")
    if not filename or not isinstance(filename, str):
        return _resp(400, {"error": "Missing field: filename"})

    limits = _plan_limits(account_id)
    if limits["attachment_bytes"] == 0:
        return _resp(403, {"error": "Attachments are not available on your plan"})

    # Keeps the key free of path traversal and header-breaking characters; the object still
    # carries the real name via the `filename` field returned alongside it.
    safe_name = SAFE_FILENAME.sub("_", filename)[-100:] or "file"
    object_key = f"users/{account_id}/{uuid.uuid4()}_{safe_name}"

    post = s3.generate_presigned_post(
        Bucket=ATTACHMENTS_BUCKET,
        Key=object_key,
        Conditions=[["content-length-range", 0, limits["attachment_bytes"]]],
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    return _resp(200, {"upload_url": post["url"], "fields": post["fields"], "object_key": object_key})


def _validate_attachments(raw, account_id, limits):
    """Checks each attachment was uploaded by this account and the total fits the plan.

    Returns (attachments, None) or (None, error_message).
    """
    if not raw:
        return [], None
    if not isinstance(raw, list):
        return None, "attachments must be a list"

    limit = limits["attachment_bytes"]
    if limit == 0:
        return None, "Attachments are not available on your plan"

    prefix = f"users/{account_id}/"
    attachments = []
    total_bytes = 0
    for entry in raw:
        object_key = entry.get("object_key") if isinstance(entry, dict) else None
        if not isinstance(object_key, str) or not object_key.startswith(prefix):
            return None, "One of the attachments doesn't belong to this account"
        try:
            head = s3.head_object(Bucket=ATTACHMENTS_BUCKET, Key=object_key)
        except ClientError as exc:
            # Same user-facing message either way, but the log tells a missing upload apart from an IAM problem.
            logger.warning(f"head_object failed for attachment | object_key={object_key} error={exc}")
            return None, "One of the attachments couldn't be found. Upload it again."
        total_bytes += head["ContentLength"]
        filename = entry.get("filename") or object_key.rsplit("/", 1)[-1]
        attachments.append({"object_key": object_key, "filename": filename})

    if total_bytes > limit:
        return None, "Attachments are too large for your plan"
    return attachments, None


def _plan_limits(account_id):
    account = ops_table.get_item(
        Key={"PK": f"ACCT#{account_id}", "SK": "META"},
        ProjectionExpression="#p", ExpressionAttributeNames={"#p": "plan"},
    ).get("Item")
    plan = (account or {}).get("plan", DEFAULT_PLAN)
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


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
