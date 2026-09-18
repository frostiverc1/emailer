import json

import pytest

from conftest import load_handler


@pytest.fixture
def h(table):
    return load_handler("admin", table)


def call(h, route, body=None, sub="a", params=None, qs=None):
    event = {"routeKey": route}
    if sub is not None:
        # What the Cognito JWT authorizer passes on.
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    if body is not None:
        event["body"] = json.dumps(body)
    if params is not None:
        event["pathParameters"] = params
    if qs is not None:
        event["queryStringParameters"] = qs
    resp = h.handler(event, None)
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None)


SERVICE = {"name": "Site", "provider_type": "ses", "ses_from_email": "hi@acme.com"}
TEMPLATE = {"template_id": "tpl_x", "subject_tpl": "Hi", "html_tpl": "<p>Hi</p>"}


def create_service(h, sub="a"):
    return call(h, "POST /admin/services", SERVICE, sub=sub)[1]["service_id"]


def test_not_logged_in(h):
    status, body = call(h, "GET /admin/services", sub=None)
    assert (status, body["error"]) == (401, "Unauthorized")


def test_service_belongs_to_caller(h, table):
    status, body = call(h, "POST /admin/services", {**SERVICE, "account_id": "acct_b"})
    assert status == 201
    stored = table.get_item(Key={"PK": f"SVC#{body['service_id']}", "SK": "META"})["Item"]
    assert stored["account_id"] == "acct_a"


def test_list_services_only_shows_own(h):
    mine = create_service(h, sub="a")
    create_service(h, sub="b")
    services = call(h, "GET /admin/services")[1]["services"]
    assert [(s["service_id"], s["account_id"]) for s in services] == [(mine, "acct_a")]


def test_templates_on_own_service(h):
    svc = create_service(h)
    assert call(h, "POST /admin/templates", {**TEMPLATE, "service_id": svc})[0] == 201
    status, body = call(h, "GET /admin/templates/{id}", params={"id": "tpl_x"}, qs={"service_id": svc})
    assert (status, body["subject_tpl"]) == (200, "Hi")


@pytest.mark.parametrize("route,params", [
    ("GET /admin/templates", None),
    ("GET /admin/templates/{id}", {"id": "tpl_x"}),
    ("PUT /admin/templates/{id}", {"id": "tpl_x"}),
    ("DELETE /admin/templates/{id}", {"id": "tpl_x"}),
])
def test_templates_on_other_accounts_service_look_missing(h, table, route, params):
    svc = create_service(h, sub="b")
    call(h, "POST /admin/templates", {**TEMPLATE, "service_id": svc}, sub="b")

    status, body = call(h, route, {"subject_tpl": "hacked"}, params=params, qs={"service_id": svc})
    assert (status, body["error"]) == (404, "Service not found")
    assert table.get_item(Key={"PK": f"SVC#{svc}", "SK": "TPL#tpl_x"})["Item"]["subject_tpl"] == "Hi"


def test_create_template_on_other_accounts_service(h):
    svc = create_service(h, sub="b")
    status, _ = call(h, "POST /admin/templates", {**TEMPLATE, "service_id": svc})
    assert status == 404


def test_usage_only_for_own_keys(h, table):
    svc = create_service(h, sub="b")
    table.put_item(Item={"PK": "APIKEY#gk_b", "SK": "META", "service_id": svc})
    assert call(h, "GET /admin/usage/{api_key}", params={"api_key": "gk_b"})[0] == 404
    assert call(h, "GET /admin/usage/{api_key}", params={"api_key": "gk_b"}, sub="b")[0] == 200


def test_email_status_only_for_own_emails(h, table):
    svc = create_service(h, sub="b")
    table.put_item(Item={"PK": "EMAIL#r1", "SK": "META", "service_id": svc, "status": "sent"})
    assert call(h, "GET /admin/emails/{request_id}", params={"request_id": "r1"})[0] == 404
    assert call(h, "GET /admin/emails/{request_id}", params={"request_id": "r1"}, sub="b")[0] == 200


def test_create_api_key_for_own_service(h, table):
    svc = create_service(h)
    status, body = call(h, "POST /admin/services/{id}/keys", {"allowed_origins": ["https://acme.com"]}, params={"id": svc})
    assert status == 201
    assert body["api_key"].startswith("gk_")
    stored = table.get_item(Key={"PK": f"APIKEY#{body['api_key']}", "SK": "META"})["Item"]
    assert (stored["service_id"], stored["active"], stored["allowed_origins"]) == (svc, True, {"https://acme.com"})


def test_api_key_without_origins(h, table):
    svc = create_service(h)
    body = call(h, "POST /admin/services/{id}/keys", params={"id": svc})[1]
    assert "allowed_origins" not in table.get_item(Key={"PK": f"APIKEY#{body['api_key']}", "SK": "META"})["Item"]


def test_api_key_bad_origins(h):
    svc = create_service(h)
    assert call(h, "POST /admin/services/{id}/keys", {"allowed_origins": "https://acme.com"}, params={"id": svc})[0] == 400


def test_api_key_for_other_accounts_service(h, table):
    svc = create_service(h, sub="b")
    status, body = call(h, "POST /admin/services/{id}/keys", params={"id": svc})
    assert (status, body["error"]) == (404, "Service not found")
