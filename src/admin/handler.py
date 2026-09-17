import json
import os
import time
import uuid
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])

# Internal-only Lambda, no auth for the pilot. Deploy behind a private stage /
# VPC link / IAM auth, see docs/email-service-pilot-lld.md section 1.


def handler(event, context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    qs = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    if route == "POST /admin/accounts":
        return _create_account(body)
    if route == "GET /admin/accounts":
        return _list_accounts()
    if route == "POST /admin/services":
        return _create_service(body)
    if route == "GET /admin/services":
        return _list_services()
    if route == "GET /admin/templates":
        return _list_templates(qs)
    if route == "POST /admin/templates":
        return _create_template(body)
    if route == "GET /admin/templates/{id}":
        return _get_template(params.get("id"), qs)
    if route == "PUT /admin/templates/{id}":
        return _update_template(params.get("id"), qs, body)
    if route == "DELETE /admin/templates/{id}":
        return _delete_template(params.get("id"), qs)
    if route == "GET /admin/usage/{api_key}":
        return _get_usage(params.get("api_key"))
    if route == "GET /admin/emails/{request_id}":
        return _get_email(params.get("request_id"))

    return _resp(404, {"error": "Unknown route"})


def _create_account(body):
    if not body.get("name"):
        return _resp(400, {"error": "Missing field: name"})

    account_id = f"acct_{uuid.uuid4().hex[:16]}"
    table.put_item(Item={
        "PK": f"ACCT#{account_id}",
        "SK": "META",
        "name": body["name"],
        "created_at": datetime.utcnow().isoformat(),
    })
    return _resp(201, {"account_id": account_id})


def _list_accounts():
    items = _scan_prefix("ACCT#", "META")
    accounts = [{
        "account_id": i["PK"].removeprefix("ACCT#"),
        "name": i.get("name"),
        "created_at": i.get("created_at"),
    } for i in items]
    return _resp(200, {"accounts": accounts})


def _create_service(body):
    for field in ("account_id", "name", "provider_type"):
        if field not in body:
            return _resp(400, {"error": f"Missing field: {field}"})
    if body["provider_type"] != "ses":
        return _resp(400, {"error": "Only provider_type=ses is supported in the pilot"})
    if not body.get("ses_from_email"):
        return _resp(400, {"error": "ses_from_email is required for provider_type=ses"})
    if "Item" not in table.get_item(Key={"PK": f"ACCT#{body['account_id']}", "SK": "META"}):
        return _resp(404, {"error": "Account not found"})

    service_id = f"svc_{uuid.uuid4().hex[:16]}"
    table.put_item(Item={
        "PK": f"SVC#{service_id}",
        "SK": "META",
        "account_id": body["account_id"],
        "name": body["name"],
        "provider_type": "ses",
        "ses_from_email": body["ses_from_email"],
        "oauth_status": "connected",
    })
    return _resp(201, {"service_id": service_id})


def _list_services():
    items = _scan_prefix("SVC#", "META")
    services = [{
        "service_id": i["PK"].removeprefix("SVC#"),
        "account_id": i.get("account_id"),
        "name": i.get("name"),
        "provider_type": i.get("provider_type"),
        "ses_from_email": i.get("ses_from_email"),
    } for i in items]
    return _resp(200, {"services": services})


def _list_templates(qs):
    service_id = qs.get("service_id")
    if not service_id:
        return _resp(400, {"error": "service_id query param is required"})

    result = table.query(
        KeyConditionExpression=Key("PK").eq(f"SVC#{service_id}") & Key("SK").begins_with("TPL#"),
    )
    templates = [{
        "template_id": i["SK"].removeprefix("TPL#"),
        "subject_tpl": i.get("subject_tpl"),
        "created_at": i.get("created_at"),
        "updated_at": i.get("updated_at"),
    } for i in result.get("Items", [])]
    return _resp(200, {"templates": templates})


def _create_template(body):
    for field in ("service_id", "template_id", "subject_tpl", "html_tpl"):
        if field not in body:
            return _resp(400, {"error": f"Missing field: {field}"})

    now = datetime.utcnow().isoformat()
    table.put_item(Item={
        "PK": f"SVC#{body['service_id']}",
        "SK": f"TPL#{body['template_id']}",
        "subject_tpl": body["subject_tpl"],
        "html_tpl": body["html_tpl"],
        "text_tpl": body.get("text_tpl"),
        "created_at": now,
        "updated_at": now,
    })
    return _resp(201, {"template_id": body["template_id"]})


def _get_template(template_id, qs):
    service_id = qs.get("service_id")
    if not service_id or not template_id:
        return _resp(400, {"error": "service_id query param and template id are required"})

    result = table.get_item(Key={"PK": f"SVC#{service_id}", "SK": f"TPL#{template_id}"})
    item = result.get("Item")
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


def _update_template(template_id, qs, body):
    service_id = qs.get("service_id")
    if not service_id or not template_id:
        return _resp(400, {"error": "service_id query param and template id are required"})

    existing = table.get_item(Key={"PK": f"SVC#{service_id}", "SK": f"TPL#{template_id}"})
    if "Item" not in existing:
        return _resp(404, {"error": "Template not found"})

    table.update_item(
        Key={"PK": f"SVC#{service_id}", "SK": f"TPL#{template_id}"},
        UpdateExpression="SET subject_tpl = :s, html_tpl = :h, text_tpl = :t, updated_at = :u",
        ExpressionAttributeValues={
            ":s": body.get("subject_tpl", existing["Item"]["subject_tpl"]),
            ":h": body.get("html_tpl", existing["Item"]["html_tpl"]),
            ":t": body.get("text_tpl", existing["Item"].get("text_tpl")),
            ":u": datetime.utcnow().isoformat(),
        },
    )
    return _resp(200, {"template_id": template_id})


def _delete_template(template_id, qs):
    service_id = qs.get("service_id")
    if not service_id or not template_id:
        return _resp(400, {"error": "service_id query param and template id are required"})

    table.delete_item(Key={"PK": f"SVC#{service_id}", "SK": f"TPL#{template_id}"})
    return _resp(204, None)


def _get_usage(api_key):
    if not api_key:
        return _resp(400, {"error": "api_key path param is required"})

    result = table.query(
        KeyConditionExpression=Key("PK").eq(f"USAGE#{api_key}") & Key("SK").begins_with("DAY#"),
    )
    days = [{
        "date": i["SK"].removeprefix("DAY#"),
        "count": int(i.get("count", 0)),
    } for i in result.get("Items", [])]
    return _resp(200, {"api_key": api_key, "usage": days})


def _get_email(request_id):
    if not request_id:
        return _resp(400, {"error": "request_id path param is required"})

    item = table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"}).get("Item")
    if not item:
        return _resp(404, {"error": "Email not found"})
    return _resp(200, {
        "request_id": request_id,
        "status": item.get("status"),
        "service_id": item.get("service_id"),
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


def _scan_prefix(pk_prefix, sk_value):
    items = []
    kwargs = {}
    while True:
        page = table.scan(**kwargs)
        items.extend(
            i for i in page.get("Items", [])
            if i["PK"].startswith(pk_prefix) and i["SK"] == sk_value
        )
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "",
    }
