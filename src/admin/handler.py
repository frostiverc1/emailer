import json
import os
import secrets
import time
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])

# Every route sits behind the Cognito JWT authorizer (infra/modules/api/auth.tf). The caller's
# account is taken from the token, never from the request, and everything is scoped to it.


def handler(event, context):
    account_id = caller_account_id(event)
    if not account_id:
        return _resp(401, {"error": "Unauthorized"})

    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    if route == "GET /admin/keys":
        return _get_current_key(account_id)
    if route == "POST /admin/keys":
        return _create_api_key(account_id, body)
    if route == "GET /admin/templates":
        return _list_templates(account_id)
    if route == "POST /admin/templates":
        return _create_template(account_id, body)
    if route == "GET /admin/templates/{id}":
        return _get_template(account_id, params.get("id"))
    if route == "PUT /admin/templates/{id}":
        return _update_template(account_id, params.get("id"), body)
    if route == "DELETE /admin/templates/{id}":
        return _delete_template(account_id, params.get("id"))
    if route == "GET /admin/usage/{api_key}":
        return _get_usage(account_id, params.get("api_key"))
    if route == "GET /admin/emails":
        return _list_emails(account_id, qs)
    if route == "GET /admin/emails/{request_id}":
        return _get_email(account_id, params.get("request_id"))

    return _resp(404, {"error": "Unknown route"})


def caller_account_id(event):
    """The logged-in user's account. One account per Cognito user for now."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    return f"acct_{claims['sub']}" if claims.get("sub") else None


def _create_api_key(account_id, body):
    origins = body.get("allowed_origins") or []
    if not isinstance(origins, list) or not all(isinstance(o, str) and o for o in origins):
        return _resp(400, {"error": "allowed_origins must be a list of origins, e.g. [\"https://acme.com\"]"})

    # One key per account: creating a key turns the previous one off, so this doubles as rotation.
    # ACCT#/CURRENT_KEY points at the live key, which avoids scanning for the account's keys.
    _revoke_current_key(account_id)

    # Same plain APIKEY# row the validate Lambda reads today. docs/api-key-tiers.md has the hashed design.
    api_key = f"gk_{secrets.token_hex(12)}"
    item = {
        "PK": f"APIKEY#{api_key}",
        "SK": "META",
        "account_id": account_id,
        "daily_limit": 500,
        "active": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    # No origins means any site can use the key. DynamoDB can't store an empty set, so leave it out.
    if origins:
        item["allowed_origins"] = set(origins)
    table.put_item(Item=item)
    table.put_item(Item={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY", "api_key": api_key})
    return _resp(201, {"api_key": api_key, "allowed_origins": origins})


def _get_current_key(account_id):
    """The account's key as a masked hint. The full key is only ever returned once, when it's created."""
    current = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    key = current and table.get_item(Key={"PK": f"APIKEY#{current['api_key']}", "SK": "META"}).get("Item")
    if not key or key.get("account_id") != account_id:
        return _resp(200, {"key": None})

    api_key = current["api_key"]
    return _resp(200, {"key": {
        "hint": f"{api_key[:3]}{'*' * 7}{api_key[-4:]}",
        "active": bool(key.get("active")),
        "created_at": key.get("created_at"),
        "allowed_origins": sorted(key.get("allowed_origins", [])),
    }})


def _revoke_current_key(account_id):
    current = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    if not current:
        return
    try:
        table.update_item(
            Key={"PK": f"APIKEY#{current['api_key']}", "SK": "META"},
            UpdateExpression="SET active = :off, revoked_at = :now",
            # Never recreate a key row that was deleted by hand.
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":off": False, ":now": datetime.utcnow().isoformat()},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def _template_key(account_id, template_id):
    return {"PK": f"ACCT#{account_id}", "SK": f"TPL#{template_id}"}


def _list_templates(account_id):
    result = table.query(
        KeyConditionExpression=Key("PK").eq(f"ACCT#{account_id}") & Key("SK").begins_with("TPL#"),
    )
    templates = [{
        "template_id": i["SK"].removeprefix("TPL#"),
        "subject_tpl": i.get("subject_tpl"),
        "created_at": i.get("created_at"),
        "updated_at": i.get("updated_at"),
    } for i in result.get("Items", [])]
    return _resp(200, {"templates": templates})


def _create_template(account_id, body):
    for field in ("template_id", "subject_tpl", "html_tpl"):
        if field not in body:
            return _resp(400, {"error": f"Missing field: {field}"})

    now = datetime.utcnow().isoformat()
    table.put_item(Item={
        **_template_key(account_id, body["template_id"]),
        "subject_tpl": body["subject_tpl"],
        "html_tpl": body["html_tpl"],
        "text_tpl": body.get("text_tpl"),
        "created_at": now,
        "updated_at": now,
    })
    return _resp(201, {"template_id": body["template_id"]})


def _get_template(account_id, template_id):
    if not template_id:
        return _resp(400, {"error": "template id is required"})

    item = table.get_item(Key=_template_key(account_id, template_id)).get("Item")
    if not item:
        return _resp(404, {"error": "Template not found"})
    return _resp(200, {
        "template_id": template_id,
        "subject_tpl": item.get("subject_tpl"),
        "html_tpl": item.get("html_tpl"),
        "text_tpl": item.get("text_tpl"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    })


def _update_template(account_id, template_id, body):
    if not template_id:
        return _resp(400, {"error": "template id is required"})

    key = _template_key(account_id, template_id)
    existing = table.get_item(Key=key)
    if "Item" not in existing:
        return _resp(404, {"error": "Template not found"})

    table.update_item(
        Key=key,
        UpdateExpression="SET subject_tpl = :s, html_tpl = :h, text_tpl = :t, updated_at = :u",
        ExpressionAttributeValues={
            ":s": body.get("subject_tpl", existing["Item"]["subject_tpl"]),
            ":h": body.get("html_tpl", existing["Item"]["html_tpl"]),
            ":t": body.get("text_tpl", existing["Item"].get("text_tpl")),
            ":u": datetime.utcnow().isoformat(),
        },
    )
    return _resp(200, {"template_id": template_id})


def _delete_template(account_id, template_id):
    if not template_id:
        return _resp(400, {"error": "template id is required"})

    table.delete_item(Key=_template_key(account_id, template_id))
    return _resp(204, None)


def _get_usage(account_id, api_key):
    if not api_key:
        return _resp(400, {"error": "api_key path param is required"})
    key = table.get_item(Key={"PK": f"APIKEY#{api_key}", "SK": "META"}).get("Item")
    if not key or key.get("account_id") != account_id:
        return _resp(404, {"error": "API key not found"})

    result = table.query(
        KeyConditionExpression=Key("PK").eq(f"USAGE#{api_key}") & Key("SK").begins_with("DAY#"),
    )
    days = [{
        "date": i["SK"].removeprefix("DAY#"),
        "count": int(i.get("count", 0)),
    } for i in result.get("Items", [])]
    return _resp(200, {"api_key": api_key, "usage": days})


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 50
EMAIL_INDEX_PREFIX = "EMAIL#"


def _list_emails(account_id, qs):
    """The account's emails, newest first, one page at a time. Pass next_cursor back as ?cursor= for the next page."""
    try:
        limit = int(qs.get("limit") or DEFAULT_PAGE_SIZE)
    except ValueError:
        return _resp(400, {"error": "limit must be a number"})
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    query = {
        "KeyConditionExpression": Key("PK").eq(f"ACCT#{account_id}") & Key("SK").begins_with(EMAIL_INDEX_PREFIX),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    cursor = qs.get("cursor")
    if cursor:
        # The cursor is just the last sort key we returned. The partition is always the caller's own.
        if not cursor.startswith(EMAIL_INDEX_PREFIX):
            return _resp(400, {"error": "Invalid cursor"})
        query["ExclusiveStartKey"] = {"PK": f"ACCT#{account_id}", "SK": cursor}

    page = table.query(**query)

    emails = []
    for entry in page.get("Items", []):
        # Status lives on the email record, which the worker keeps up to date. A missing or foreign
        # record (expired a moment before its index row, say) is skipped rather than shown wrong.
        item = table.get_item(Key={"PK": f"EMAIL#{entry['request_id']}", "SK": "META"}).get("Item")
        if item and item.get("account_id") == account_id:
            emails.append(_email_summary(entry["request_id"], item))

    last = page.get("LastEvaluatedKey")
    return _resp(200, {"emails": emails, "next_cursor": last["SK"] if last else None})


def _email_summary(request_id, item):
    return {
        "request_id": request_id,
        "status": item.get("status"),
        "template_id": item.get("template_id"),
        "to_email": item.get("to_email"),
        "created_at": item.get("created_at"),
        "sent_at": item.get("sent_at"),
        "failed_at": item.get("failed_at"),
        "error_code": item.get("error_code"),
    }


def _get_email(account_id, request_id):
    if not request_id:
        return _resp(400, {"error": "request_id path param is required"})

    item = table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"}).get("Item")
    if not item or item.get("account_id") != account_id:
        return _resp(404, {"error": "Email not found"})
    return _resp(200, {
        "request_id": request_id,
        "status": item.get("status"),
        "template_id": item.get("template_id"),
        "to_email": item.get("to_email"),
        "attempts": int(item.get("attempts", 0)),
        "created_at": item.get("created_at"),
        "sent_at": item.get("sent_at"),
        "failed_at": item.get("failed_at"),
        "ses_message_id": item.get("ses_message_id"),
        "error_code": item.get("error_code"),
        "error_message": item.get("error_message"),
    })


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "",
    }
