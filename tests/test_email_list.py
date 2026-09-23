import json
from unittest import mock

import pytest

from conftest import load_handler

KEY_ID = "test-key-id"


@pytest.fixture
def admin(table):
    return load_handler("admin", table)


@pytest.fixture
def validate(table, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEYID#{KEY_ID}", "SK": "META", "account_id": "acct_a"})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def send(validate, to="u@example.com"):
    body = {"from_email": "hi@acme.com", "template_id": "tpl_x", "template_params": {"to_email": to}}
    event = {"httpMethod": "POST", "resource": "/v1/send", "requestContext": {"identity": {"apiKeyId": KEY_ID}}, "body": json.dumps(body)}
    resp = validate.handler(event, None)
    assert resp["statusCode"] == 202
    return json.loads(resp["body"])["request_id"]


def list_emails(admin, sub="a", **qs):
    event = {
        "httpMethod": "GET",
        "resource": "/admin/emails",
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
        "queryStringParameters": qs or None,
    }
    resp = admin.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def put_email(table, account, request_id, created_at, status="queued", **extra):
    """An email as validate writes it: the record itself, plus the pointer row the list reads."""
    table.put_item(Item={
        "PK": f"EMAIL#{request_id}", "SK": "META", "account_id": account, "status": status,
        "template_id": "tpl_x", "to_email": f"{request_id}@example.com", "created_at": created_at, **extra,
    })
    table.put_item(Item={"PK": f"ACCT#{account}", "SK": f"EMAIL#{created_at}#{request_id}", "request_id": request_id})


def test_a_sent_email_shows_up_in_the_list(admin, validate):
    request_id = send(validate)
    status, body = list_emails(admin)
    assert status == 200
    assert [e["request_id"] for e in body["emails"]] == [request_id]
    assert body["emails"][0]["status"] == "queued"
    assert (body["emails"][0]["to_email"], body["emails"][0]["template_id"]) == ("u@example.com", "tpl_x")
    assert body["next_cursor"] is None


def test_index_row_expires_with_the_email_record(validate, table):
    request_id = send(validate)
    record = table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"})["Item"]
    index = table.get_item(Key={"PK": "ACCT#acct_a", "SK": f"EMAIL#{record['created_at']}#{request_id}"})["Item"]
    assert index["ttl"] == record["ttl"]


def test_list_shows_the_workers_latest_status(admin, table):
    put_email(table, "acct_a", "r1", "2026-09-21T10:00:00.000000Z")
    table.update_item(
        Key={"PK": "EMAIL#r1", "SK": "META"},
        UpdateExpression="SET #s = :s, sent_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "sent", ":t": "2026-09-21T10:00:05.000000Z"},
    )
    email = list_emails(admin)[1]["emails"][0]
    assert (email["status"], email["sent_at"]) == ("sent", "2026-09-21T10:00:05.000000Z")


def test_failed_emails_carry_their_error_code(admin, table):
    put_email(table, "acct_a", "r1", "2026-09-21T10:00:00.000000Z", status="failed", error_code="SES_REJECTED")
    assert list_emails(admin)[1]["emails"][0]["error_code"] == "SES_REJECTED"


def test_newest_first(admin, table):
    put_email(table, "acct_a", "old", "2026-09-21T09:00:00.000000Z")
    put_email(table, "acct_a", "new", "2026-09-21T11:00:00.000000Z")
    put_email(table, "acct_a", "mid", "2026-09-21T10:00:00.000000Z")
    assert [e["request_id"] for e in list_emails(admin)[1]["emails"]] == ["new", "mid", "old"]


def test_pages_cover_everything_once(admin, table):
    for i in range(5):
        put_email(table, "acct_a", f"r{i}", f"2026-09-21T10:00:0{i}.000000Z")

    seen, cursor, pages = [], None, 0
    while True:
        qs = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = list_emails(admin, **qs)[1]
        seen += [e["request_id"] for e in body["emails"]]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert seen == ["r4", "r3", "r2", "r1", "r0"]
    assert pages == 3


def test_only_your_own_emails(admin, table):
    put_email(table, "acct_a", "mine", "2026-09-21T10:00:00.000000Z")
    put_email(table, "acct_b", "theirs", "2026-09-21T10:00:01.000000Z")
    assert [e["request_id"] for e in list_emails(admin, sub="a")[1]["emails"]] == ["mine"]
    assert [e["request_id"] for e in list_emails(admin, sub="b")[1]["emails"]] == ["theirs"]


def test_a_cursor_from_another_account_cannot_reach_their_emails(admin, table):
    put_email(table, "acct_a", "mine", "2026-09-21T10:00:00.000000Z")
    put_email(table, "acct_b", "theirs", "2026-09-21T10:00:01.000000Z")
    _, page = list_emails(admin, sub="b", limit="1")
    body = list_emails(admin, sub="a", cursor=f"EMAIL#{'2026-09-21T10:00:01.000000Z'}#theirs")[1]
    assert page["emails"][0]["request_id"] == "theirs"
    assert [e["request_id"] for e in body["emails"]] == ["mine"]


def test_index_row_without_a_record_is_skipped(admin, table):
    put_email(table, "acct_a", "kept", "2026-09-21T10:00:00.000000Z")
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "EMAIL#2026-09-21T11:00:00.000000Z#gone", "request_id": "gone"})
    assert [e["request_id"] for e in list_emails(admin)[1]["emails"]] == ["kept"]


def test_index_row_pointing_at_someone_elses_record_is_skipped(admin, table):
    put_email(table, "acct_b", "theirs", "2026-09-21T10:00:00.000000Z")
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "EMAIL#2026-09-21T10:00:00.000000Z#theirs", "request_id": "theirs"})
    assert list_emails(admin, sub="a")[1]["emails"] == []


def test_other_rows_in_the_account_partition_are_not_listed(admin, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "TPL#tpl_x", "subject_tpl": "Hi"})
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "DOMAIN#acme.com", "domain": "acme.com"})
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "CURRENT_KEY", "api_key_id": "test-key-id"})
    assert list_emails(admin)[1] == {"emails": [], "next_cursor": None}


def test_empty_list(admin):
    assert list_emails(admin) == (200, {"emails": [], "next_cursor": None})


@pytest.mark.parametrize("qs", [{"cursor": "TPL#x"}, {"cursor": "garbage"}, {"limit": "many"}])
def test_bad_query_params(admin, qs):
    assert list_emails(admin, **qs)[0] == 400


def test_limit_is_capped(admin, table):
    for i in range(55):
        put_email(table, "acct_a", f"r{i:02d}", f"2026-09-21T10:{i:02d}:00.000000Z")
    body = list_emails(admin, limit="1000")[1]
    assert len(body["emails"]) == 50
    assert body["next_cursor"] is not None


def test_not_logged_in(admin):
    resp = admin.handler({"httpMethod": "GET", "resource": "/admin/emails"}, None)
    assert resp["statusCode"] == 401


def test_a_failing_index_write_does_not_stop_the_send(validate, table):
    class IndexBroken:
        def __getattr__(self, name):
            return getattr(table, name)

        def put_item(self, **kwargs):
            if kwargs["Item"]["PK"].startswith("ACCT#"):
                raise RuntimeError("dynamodb hiccup")
            return table.put_item(**kwargs)

    validate.ops_table = IndexBroken()
    request_id = send(validate)
    assert table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"})["Item"]["status"] == "queued"
    validate.sqs.send_message.assert_called_once()


def lookup(admin, request_id, sub):
    event = {
        "httpMethod": "GET",
        "resource": "/admin/emails/{request_id}",
        "pathParameters": {"request_id": request_id},
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }
    return admin.handler(event, None)


def test_someone_elses_request_id_looks_exactly_like_one_that_does_not_exist(admin, table):
    put_email(table, "acct_b", "theirs", "2026-09-21T10:00:00.000000Z")

    foreign = lookup(admin, "theirs", sub="a")
    missing = lookup(admin, "no-such-id", sub="a")
    assert foreign == missing
    assert foreign["statusCode"] == 404

    # And the owner does see it.
    assert lookup(admin, "theirs", sub="b")["statusCode"] == 200


def test_foreign_email_details_never_appear_in_any_response_to_another_account(admin, table):
    put_email(table, "acct_b", "theirs", "2026-09-21T10:00:00.000000Z", status="sent")
    responses = [lookup(admin, "theirs", sub="a")["body"], json.dumps(list_emails(admin, sub="a")[1])]
    for body in responses:
        assert "theirs" not in body
        assert "acct_b" not in body


def test_emails_from_before_accounts_existed_are_visible_to_nobody(admin, table):
    table.put_item(Item={"PK": "EMAIL#old", "SK": "META", "service_id": "svc_old", "status": "sent", "to_email": "x@example.com"})
    assert lookup(admin, "old", sub="a")["statusCode"] == 404
    assert lookup(admin, "old", sub="b")["statusCode"] == 404
