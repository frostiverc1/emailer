import json
import logging
import os
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
apigw = boto3.client("apigateway")

# Every admin route sits behind the Cognito authorizer (infra/modules/api/auth.tf). The caller's
# account is taken from the token, never from the request, and everything is scoped to it.

# No billing yet, so every account sits on "free" until a plan is set on its ACCT#/META row.
# Kept in sync with the same table in src/validate/handler.py (each Lambda is packaged separately).
DEFAULT_PLAN = "free"
PLANS = {
    "free":         {"price_usd": 0,  "requests_per_month": 200,   "templates": 2,    "attachment_bytes": 0,                "retention_days": 7,  "rate_limit": 2,  "burst_limit": 5},
    "personal":     {"price_usd": 9,  "requests_per_month": 2000,  "templates": 6,    "attachment_bytes": 500 * 1024,       "retention_days": 30, "rate_limit": 5,  "burst_limit": 10},
    "professional": {"price_usd": 15, "requests_per_month": 5000,  "templates": None, "attachment_bytes": 2 * 1024 * 1024,  "retention_days": 30, "rate_limit": 10, "burst_limit": 20},
    "business":     {"price_usd": 40, "requests_per_month": 25000, "templates": None, "attachment_bytes": 25 * 1024 * 1024, "retention_days": 30, "rate_limit": 25, "burst_limit": 50},
}

# API Gateway usage plans are named "<this prefix><tier>" (infra/modules/api/main.tf). Looked up by
# name and cached, rather than passed in as an env var, to avoid a Terraform dependency cycle
# (the usage plans' api_stages block depends on the deployment, which depends on this Lambda).
USAGE_PLAN_NAME_PREFIX = os.environ.get("USAGE_PLAN_NAME_PREFIX", "")
_usage_plan_ids = {}


def _usage_plan_id(plan):
    if not _usage_plan_ids:
        for up in apigw.get_usage_plans(limit=500).get("items", []):
            name = up.get("name", "")
            if name.startswith(USAGE_PLAN_NAME_PREFIX):
                _usage_plan_ids[name[len(USAGE_PLAN_NAME_PREFIX):]] = up["id"]
    return _usage_plan_ids.get(plan)


def _current_plan(account_id):
    account = table.get_item(
        Key={"PK": f"ACCT#{account_id}", "SK": "META"},
        ProjectionExpression="#p", ExpressionAttributeNames={"#p": "plan"},
    ).get("Item")
    plan = (account or {}).get("plan", DEFAULT_PLAN)
    return plan if plan in PLANS else DEFAULT_PLAN


def _plan_limits(account_id):
    return PLANS[_current_plan(account_id)]


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


def _set_plan(account_id, body):
    plan = body.get("plan")
    if plan not in PLANS:
        return _resp(400, {"error": f"plan must be one of: {', '.join(PLANS)}"})
    table.update_item(
        Key={"PK": f"ACCT#{account_id}", "SK": "META"},
        UpdateExpression="SET #p = :plan",
        ExpressionAttributeNames={"#p": "plan"},
        ExpressionAttributeValues={":plan": plan},
    )
    _move_key_to_plan(account_id, plan)
    return _resp(200, _plan_snapshot(account_id))


def _usage_this_month(usage_plan_id, key_id):
    """Best effort: this is a display number, so a lookup failure just shows 0 instead of erroring."""
    if not usage_plan_id or not key_id:
        return 0
    today = datetime.utcnow().date()
    try:
        usage = apigw.get_usage(
            usagePlanId=usage_plan_id, keyId=key_id,
            startDate=today.replace(day=1).isoformat(), endDate=today.isoformat(),
        )
        return sum(day[0] for day in usage.get("items", {}).get(key_id, []))
    except Exception:
        logger.exception(f"Failed to read usage | key_id={key_id}")
        return 0


def _plan_snapshot(account_id):
    plan = _current_plan(account_id)

    current_key = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    requests_this_month = 0
    if current_key:
        requests_this_month = _usage_this_month(_usage_plan_id(plan), current_key["api_key_id"])

    templates_used = table.query(
        KeyConditionExpression=Key("PK").eq(f"ACCT#{account_id}") & Key("SK").begins_with("TPL#"),
        Select="COUNT",
    )["Count"]

    return {
        "plan": plan,
        "limits": PLANS[plan],
        "usage": {"requests_this_month": requests_this_month, "templates_used": templates_used},
        # The full catalog, so the frontend can show all plans without duplicating these numbers.
        "catalog": PLANS,
    }


def handler(event, context):
    route = f"{event.get('httpMethod')} {event.get('resource')}"

    account_id = caller_account_id(event)
    if not account_id:
        return _resp(401, {"error": "Unauthorized"})

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
    if route == "GET /admin/emails":
        return _list_emails(account_id, qs)
    if route == "GET /admin/emails/{request_id}":
        return _get_email(account_id, params.get("request_id"))
    if route == "GET /admin/plan":
        return _resp(200, _plan_snapshot(account_id))
    if route == "POST /admin/plan":
        return _set_plan(account_id, body)

    return _resp(404, {"error": "Unknown route"})


def caller_account_id(event):
    """The logged-in user's account. One account per Cognito user for now."""
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    return f"acct_{claims['sub']}" if claims.get("sub") else None


def _create_api_key(account_id, body):
    origins = body.get("allowed_origins") or []
    if not isinstance(origins, list) or not all(isinstance(o, str) and o for o in origins):
        return _resp(400, {"error": "allowed_origins must be a list of origins, e.g. [\"https://acme.com\"]"})

    # One key per account: creating a key turns the previous one off, so this doubles as rotation.
    # ACCT#/CURRENT_KEY points at the live key, which avoids scanning for the account's keys.
    _revoke_current_key(account_id)

    key = apigw.create_api_key(name=f"{account_id}", enabled=True)
    usage_plan_id = _usage_plan_id(_current_plan(account_id))
    if usage_plan_id:
        apigw.create_usage_plan_key(usagePlanId=usage_plan_id, keyId=key["id"], keyType="API_KEY")

    # Non-secret: only the key's id, which the request carries as requestContext.identity.apiKeyId.
    # API Gateway holds the actual secret value; we never store it.
    item = {
        "PK": f"APIKEYID#{key['id']}",
        "SK": "META",
        "account_id": account_id,
        "created_at": datetime.utcnow().isoformat(),
    }
    # No origins means any site can use the key. DynamoDB can't store an empty set, so leave it out.
    if origins:
        item["allowed_origins"] = set(origins)
    table.put_item(Item=item)
    table.put_item(Item={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY", "api_key_id": key["id"]})
    return _resp(201, {"api_key": key["value"], "allowed_origins": origins})


def _get_current_key(account_id):
    """The account's key as a masked hint. The full key is only ever returned once, when it's created."""
    current = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    meta = current and table.get_item(Key={"PK": f"APIKEYID#{current.get('api_key_id')}", "SK": "META"}).get("Item")
    if not meta or meta.get("account_id") != account_id:
        return _resp(200, {"key": None})

    try:
        gw_key = apigw.get_api_key(apiKey=current["api_key_id"], includeValue=True)
    except ClientError:
        return _resp(200, {"key": None})

    value = gw_key.get("value", "")
    return _resp(200, {"key": {
        "hint": f"{value[:3]}{'*' * 7}{value[-4:]}" if len(value) > 7 else "*" * len(value),
        "active": bool(gw_key.get("enabled")),
        "created_at": meta.get("created_at"),
        "allowed_origins": sorted(meta.get("allowed_origins", [])),
    }})


def _revoke_current_key(account_id):
    current = table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": "CURRENT_KEY"}).get("Item")
    key_id = current and current.get("api_key_id")
    if not key_id:
        return
    try:
        apigw.delete_api_key(apiKey=key_id)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NotFoundException":
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

    # POST also updates an existing template_id, so the limit only applies to genuinely new ones.
    is_new = "Item" not in table.get_item(Key=_template_key(account_id, body["template_id"]), ProjectionExpression="PK")
    if is_new:
        limit = _plan_limits(account_id)["templates"]
        if limit is not None:
            count = table.query(
                KeyConditionExpression=Key("PK").eq(f"ACCT#{account_id}") & Key("SK").begins_with("TPL#"),
                Select="COUNT",
            )["Count"]
            if count >= limit:
                return _resp(403, {"error": f"Template limit reached for your plan ({limit})"})

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
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body) if body is not None else "",
    }
