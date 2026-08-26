import json
import os
import time
import uuid
from datetime import date, datetime

import boto3

ops_table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
suppression_table = boto3.resource("dynamodb").Table(os.environ["SUPPRESSION_TABLE_NAME"])
sqs = boto3.client("sqs")
QUEUE_URL = os.environ["QUEUE_URL"]


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

    for field in ("service_id", "template_id", "template_params"):
        if field not in body:
            return _resp(400, {"error": f"Missing field: {field}"})

    to_email = body["template_params"].get("to_email")
    if not to_email:
        return _resp(400, {"error": "template_params.to_email is required"})

    # --- 1. look up API key (ops table) ---
    key_record = ops_table.get_item(
        Key={"PK": f"APIKEY#{api_key}", "SK": "META"},
        ProjectionExpression="service_id, allowed_origins, daily_limit, active",
    )
    item = key_record.get("Item")
    if not item or not item.get("active", False):
        return _resp(401, {"error": "Invalid API key"})

    # --- 2. origin check ---
    allowed = item.get("allowed_origins", set())
    if allowed and origin and origin not in allowed:
        return _resp(403, {"error": "Origin not allowed"})

    # --- 3. service_id must match key ---
    if body["service_id"] != item["service_id"]:
        return _resp(403, {"error": "API key not authorized for this service"})

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

    # --- 5. bounce suppression check (suppression table) ---
    suppression = suppression_table.get_item(
        Key={"PK": f"BOUNCE#{to_email}", "SK": "META"},
        ProjectionExpression="suppressed",
    )
    if suppression.get("Item", {}).get("suppressed", False):
        return _resp(422, {"error": "Recipient suppressed due to bounces"})

    # --- 6. queue the job ---
    request_id = str(uuid.uuid4())
    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({
            "request_id": request_id,
            "api_key": api_key,
            "service_id": body["service_id"],
            "template_id": body["template_id"],
            "template_params": body["template_params"],
            "queued_at": datetime.utcnow().isoformat(),
        }),
    )

    return _resp(202, {"status": "queued", "request_id": request_id})


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
