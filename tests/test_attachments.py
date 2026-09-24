import json
from unittest import mock

import boto3
import pytest

from conftest import ATTACHMENTS_BUCKET, load_handler

KEY_ID = "test-key-id"


@pytest.fixture
def bucket(table):
    # us-east-1 is the region moto's client defaults to; no LocationConstraint needed there.
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=ATTACHMENTS_BUCKET)
    return boto3.client("s3", region_name="us-east-1")


# --- validate: POST /v1/attachments/upload-url ---

@pytest.fixture
def v(table, bucket, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.test/queue")
    module = load_handler("validate", table)
    module.ops_table = table
    module.sqs = mock.MagicMock()
    table.put_item(Item={"PK": f"APIKEYID#{KEY_ID}", "SK": "META", "account_id": "acct_a"})
    table.put_item(Item={"PK": "DOMAIN#acme.com", "SK": "META", "account_id": "acct_a", "status": "verified"})
    return module


def upload_url(v, api_key_id=KEY_ID, **body_overrides):
    body = {"filename": "invoice.pdf", **body_overrides}
    event = {
        "httpMethod": "POST",
        "resource": "/v1/attachments/upload-url",
        "requestContext": {"identity": {"apiKeyId": api_key_id} if api_key_id else {}},
        "body": json.dumps(body),
    }
    resp = v.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_upload_url_blocked_on_free_plan(v):
    status, body = upload_url(v)
    assert (status, body["error"]) == (403, "Attachments are not available on your plan")


def test_upload_url_issued_on_a_paying_plan(v, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    status, body = upload_url(v)
    assert status == 200
    assert body["object_key"].startswith("users/acct_a/")
    assert body["upload_url"]
    assert body["fields"]


def test_upload_url_sanitizes_the_filename(v, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    status, body = upload_url(v, filename="../../etc/passwd")
    assert status == 200
    assert "/" not in body["object_key"].removeprefix("users/acct_a/")


def test_upload_url_requires_api_key(v):
    assert upload_url(v, api_key_id=None)[0] == 401


def test_upload_url_requires_filename(v, table):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    status, body = upload_url(v, filename="")
    assert (status, body["error"]) == (400, "Missing field: filename")


# --- validate: POST /v1/send with attachments ---

def send(v, **overrides):
    body = {"from": "hi@acme.com", "to": "u@example.com", "template_id": "tpl_x", "template_params": {}}
    body.update(overrides)
    event = {
        "httpMethod": "POST",
        "resource": "/v1/send",
        "requestContext": {"identity": {"apiKeyId": KEY_ID}},
        "body": json.dumps(body),
    }
    resp = v.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


def put_object(bucket_client, account_id, filename, size):
    key = f"users/{account_id}/{filename}"
    bucket_client.put_object(Bucket=ATTACHMENTS_BUCKET, Key=key, Body=b"x" * size)
    return key


def test_send_with_a_valid_attachment_is_queued(v, table, bucket):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    key = put_object(bucket, "acct_a", "f1", 100)
    status, body = send(v, attachments=[{"object_key": key, "filename": "f1.pdf"}])
    assert (status, body["status"]) == (202, "queued")
    queued = json.loads(v.sqs.send_message.call_args.kwargs["MessageBody"])
    assert queued["attachments"] == [{"object_key": key, "filename": "f1.pdf"}]


def test_send_rejects_attachment_on_free_plan(v, table, bucket):
    key = put_object(bucket, "acct_a", "f1", 100)
    status, body = send(v, attachments=[{"object_key": key}])
    assert (status, body["error"]) == (403, "Attachments are not available on your plan")
    v.sqs.send_message.assert_not_called()


def test_send_rejects_attachment_belonging_to_another_account(v, table, bucket):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    key = put_object(bucket, "acct_b", "f1", 100)
    status, body = send(v, attachments=[{"object_key": key}])
    assert status == 403
    assert "doesn't belong" in body["error"]
    v.sqs.send_message.assert_not_called()


def test_send_rejects_attachments_over_the_plan_total(v, table, bucket):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})  # 500KB cap
    key = put_object(bucket, "acct_a", "f1", 600 * 1024)
    status, body = send(v, attachments=[{"object_key": key}])
    assert (status, body["error"]) == (403, "Attachments are too large for your plan")
    v.sqs.send_message.assert_not_called()


def test_send_rejects_a_missing_attachment(v, table, bucket):
    table.put_item(Item={"PK": "ACCT#acct_a", "SK": "META", "plan": "personal"})
    status, body = send(v, attachments=[{"object_key": "users/acct_a/nope"}])
    assert status == 403
    assert "couldn't be found" in body["error"]


# --- worker: sending and cleaning up attachments ---

@pytest.fixture
def w(table, bucket, monkeypatch):
    monkeypatch.setenv("MAX_RECEIVE_COUNT", "3")
    monkeypatch.setenv("LEASE_SECONDS", "90")
    module = load_handler("worker", table)
    module.table = table
    module.ses = mock.MagicMock(**{"send_email.return_value": {"MessageId": "msg-0"}})
    module.sesv2 = mock.MagicMock(**{"send_email.return_value": {"MessageId": "msg-1"}})
    table.put_item(Item={
        "PK": "ACCT#acct_a", "SK": "TPL#tpl_x",
        "subject_tpl": "Hi", "html_tpl": "<p>Hi {{ to_name }}</p>", "text_tpl": "Hi {{ to_name }}",
    })
    return module


def sqs_record(request_id, attempt=1):
    return {"body": "placeholder", "attributes": {"ApproximateReceiveCount": str(attempt)}}


def make_message(request_id, attachments, **overrides):
    message = {
        "request_id": request_id,
        "api_key_id": KEY_ID,
        "account_id": "acct_a",
        "from": "hi@acme.com",
        "to": "u@example.com",
        "template_id": "tpl_x",
        "template_params": {"to_name": "Sam"},
        "attachments": attachments,
    }
    message.update(overrides)
    return message


def test_worker_sends_attachments_via_sesv2_and_deletes_them_on_success(w, table, bucket):
    request_id = "r1"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    key = put_object(bucket, "acct_a", "f1", 10)
    record = sqs_record(request_id)
    record["body"] = json.dumps(make_message(request_id, [{"object_key": key, "filename": "f1.pdf"}]))

    w.handler({"Records": [record]}, None)

    w.sesv2.send_email.assert_called_once()
    w.ses.send_email.assert_not_called()
    raw = w.sesv2.send_email.call_args.kwargs["Content"]["Raw"]["Data"]
    assert b"f1.pdf" in raw
    with pytest.raises(bucket.exceptions.NoSuchKey):
        bucket.get_object(Bucket=ATTACHMENTS_BUCKET, Key=key)
    status = table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"})["Item"]["status"]
    assert status == "sent"


def test_worker_keeps_the_attachment_for_a_retryable_failure(w, table, bucket):
    request_id = "r2"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    key = put_object(bucket, "acct_a", "f1", 10)
    w.sesv2.send_email.side_effect = w.ClientError(
        {"Error": {"Code": "Throttling", "Message": "slow down"}}, "SendEmail"
    )
    record = sqs_record(request_id, attempt=1)
    record["body"] = json.dumps(make_message(request_id, [{"object_key": key, "filename": "f1.pdf"}]))

    with pytest.raises(Exception):
        w.handler({"Records": [record]}, None)

    # Still there: a retry needs it.
    bucket.get_object(Bucket=ATTACHMENTS_BUCKET, Key=key)


def test_worker_deletes_the_attachment_after_a_final_failure(w, table, bucket):
    request_id = "r3"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    key = put_object(bucket, "acct_a", "f1", 10)
    w.sesv2.send_email.side_effect = w.ClientError(
        {"Error": {"Code": "MessageRejected", "Message": "bad address"}}, "SendEmail"
    )
    record = sqs_record(request_id, attempt=1)
    record["body"] = json.dumps(make_message(request_id, [{"object_key": key, "filename": "f1.pdf"}]))

    w.handler({"Records": [record]}, None)

    with pytest.raises(bucket.exceptions.NoSuchKey):
        bucket.get_object(Bucket=ATTACHMENTS_BUCKET, Key=key)


# --- worker: reply_to, and the from/to top-level fields ---

def test_worker_uses_the_top_level_from_and_to(w, table, bucket):
    request_id = "r4"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    record = sqs_record(request_id)
    record["body"] = json.dumps(make_message(request_id, [], **{"from": "hi@acme.com", "to": "u@example.com"}))

    w.handler({"Records": [record]}, None)

    assert w.ses.send_email.call_args.kwargs["Source"] == "hi@acme.com"
    assert w.ses.send_email.call_args.kwargs["Destination"] == {"ToAddresses": ["u@example.com"]}
    assert table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"})["Item"]["status"] == "sent"


def test_worker_sets_reply_to_addresses_without_attachments(w, table, bucket):
    request_id = "r5"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    record = sqs_record(request_id)
    record["body"] = json.dumps(make_message(request_id, [], reply_to="visitor@example.com"))

    w.handler({"Records": [record]}, None)

    assert w.ses.send_email.call_args.kwargs["ReplyToAddresses"] == ["visitor@example.com"]


def test_worker_omits_reply_to_addresses_when_not_given(w, table, bucket):
    request_id = "r6"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    record = sqs_record(request_id)
    record["body"] = json.dumps(make_message(request_id, []))

    w.handler({"Records": [record]}, None)

    assert "ReplyToAddresses" not in w.ses.send_email.call_args.kwargs


def test_worker_puts_reply_to_in_the_raw_message_header_when_there_are_attachments(w, table, bucket):
    request_id = "r7"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    key = put_object(bucket, "acct_a", "f1", 10)
    record = sqs_record(request_id)
    record["body"] = json.dumps(
        make_message(request_id, [{"object_key": key, "filename": "f1.pdf"}], reply_to="visitor@example.com")
    )

    w.handler({"Records": [record]}, None)

    raw = w.sesv2.send_email.call_args.kwargs["Content"]["Raw"]["Data"]
    assert b"Reply-To: visitor@example.com" in raw


def test_worker_fails_a_message_missing_the_to_field(w, table):
    request_id = "r8"
    table.put_item(Item={"PK": f"EMAIL#{request_id}", "SK": "META", "status": "queued", "attempts": 0})
    message = make_message(request_id, [])
    del message["to"]
    record = sqs_record(request_id)
    record["body"] = json.dumps(message)

    w.handler({"Records": [record]}, None)

    item = table.get_item(Key={"PK": f"EMAIL#{request_id}", "SK": "META"})["Item"]
    assert (item["status"], item["error_code"]) == ("failed", "INVALID_MESSAGE")
    w.ses.send_email.assert_not_called()
