import json
import os
import logging
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import boto3
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from jinja2 import Template, TemplateSyntaxError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "us-east-1"))
# Attachments need the v2 API: SendRawEmail (v1) caps a message at 10MB, sesv2 at 40MB.
sesv2 = boto3.client("sesv2", region_name=os.environ.get("SES_REGION", "us-east-1"))
s3 = boto3.client("s3")
ATTACHMENTS_BUCKET = os.environ.get("ATTACHMENTS_BUCKET_NAME")

MAX_RECEIVE_COUNT = int(os.environ["MAX_RECEIVE_COUNT"])
LEASE_SECONDS = int(os.environ["LEASE_SECONDS"])
# Same fixed-width format validate writes, so lease timestamps compare correctly as strings.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

PERMANENT_SES_ERRORS = {
    "MessageRejected": "SES_REJECTED",
    "MailFromDomainNotVerifiedException": "SES_REJECTED",
    "ConfigurationSetDoesNotExist": "SES_REJECTED",
    "InvalidParameterValue": "SES_REJECTED",
    "AccountSendingPausedException": "SES_ACCOUNT_PAUSED",  # v1 (ses.send_email)
    "AccountSuspendedException": "SES_ACCOUNT_PAUSED",      # v2 (sesv2.send_email, used for attachments)
    "SendingPausedException": "SES_ACCOUNT_PAUSED",         # v2
}
TRANSIENT_SES_ERRORS = {"Throttling", "ServiceUnavailable", "InternalFailure"}
TRANSIENT_DYNAMODB_ERRORS = {
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
    "InternalServerError",
}
# SES reports both sending-rate and daily-quota limits as "Throttling"; only the message tells them apart.
DAILY_QUOTA_MARKER = "daily message quota exceeded"


class PermanentError(Exception):
    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code


def handler(event, context):
    for record in event["Records"]:
        _handle_record(record)


def _handle_record(record):
    try:
        msg = json.loads(record["body"])
        request_id = msg["request_id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error(f"Dropping unparseable message | error_code=INVALID_MESSAGE error={exc}")
        return

    key = {"PK": f"EMAIL#{request_id}", "SK": "META"}
    if not _claim(key, request_id):
        return

    receive_count = int(record["attributes"]["ApproximateReceiveCount"])
    try:
        message_id = _process(msg)
    except Exception as exc:
        error_code, retryable = _classify(exc)
        will_retry = retryable and receive_count < MAX_RECEIVE_COUNT
        log = logger.warning if will_retry else logger.error
        log(
            f"Send failed | request_id={request_id} error_code={error_code} "
            f"retryable={retryable} attempt={receive_count} error={exc}"
        )
        _record_error(key, request_id, "queued" if will_retry else "failed", error_code, exc)
        if not will_retry:
            # A retry still needs the attachment, so only clean up once nothing will try again.
            _cleanup_attachments(msg.get("attachments"))
        if retryable:
            raise  # SQS redelivers, or moves the message to the DLQ on the last attempt
        return

    _mark_sent(key, request_id, message_id)
    _cleanup_attachments(msg.get("attachments"))


def _claim(key, request_id):
    now = datetime.now(timezone.utc)
    try:
        table.update_item(
            Key=key,
            UpdateExpression="SET #s = :sending, sending_started_at = :now ADD attempts :one",
            ConditionExpression="#s = :queued OR (#s = :sending AND sending_started_at < :cutoff)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":sending": "sending",
                ":queued": "queued",
                ":now": now.strftime(TS_FORMAT),
                ":cutoff": (now - timedelta(seconds=LEASE_SECONDS)).strftime(TS_FORMAT),
                ":one": 1,
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    item = table.get_item(
        Key=key, ProjectionExpression="#s", ExpressionAttributeNames={"#s": "status"}
    ).get("Item")
    if not item:
        logger.error(f"Dropping message with no email record | request_id={request_id} error_code=MISSING_RECORD")
    else:
        logger.info(f"Skipping email already claimed | request_id={request_id} status={item['status']}")
    return False


def _process(msg):
    for field in ("api_key_id", "account_id", "from_email", "template_id", "template_params"):
        if field not in msg:
            raise PermanentError("INVALID_MESSAGE", f"Missing field: {field}")
    params = msg["template_params"]
    if not isinstance(params, dict) or not params.get("to_email"):
        raise PermanentError("INVALID_MESSAGE", "template_params.to_email is required")

    tpl_id = msg["template_id"]
    request_id = msg["request_id"]

    # 1. fetch template
    tpl = table.get_item(
        Key={"PK": f"ACCT#{msg['account_id']}", "SK": f"TPL#{tpl_id}"},
        ProjectionExpression="subject_tpl, html_tpl, text_tpl",
    )
    if "Item" not in tpl:
        raise PermanentError("TEMPLATE_NOT_FOUND", f"Template not found: {tpl_id}")

    t = tpl["Item"]

    # 2. render with Jinja2
    subject = Template(t["subject_tpl"]).render(**params)
    # Params come from end users (e.g. a website form), so they must not inject HTML. Subject and text
    # are plain text and stay unescaped. A template can still opt out per value with | safe.
    html_body = Template(t["html_tpl"], autoescape=True).render(**params)
    text_body = Template(t["text_tpl"]).render(**params) if t.get("text_tpl") else None

    # 3. send via SES. validate already checked from_email is on one of the account's verified domains.
    to_email = params["to_email"]
    attachments = msg.get("attachments") or []
    message_id = _send_via_ses(
        msg["from_email"], to_email, subject, html_body, text_body, request_id, msg["api_key_id"], attachments,
        reply_to=msg.get("reply_to"),
    )

    logger.info(f"Sent email | request_id={request_id} to={to_email}")
    return message_id


def _classify(exc):
    if isinstance(exc, PermanentError):
        return exc.error_code, False
    if isinstance(exc, TemplateSyntaxError):
        return "TEMPLATE_RENDER_ERROR", False
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
        return "NETWORK", True
    if isinstance(exc, ClientError):
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"].get("Message", "")
        if code == "Throttling" and DAILY_QUOTA_MARKER in message.lower():
            return "SES_DAILY_QUOTA", False
        if code in PERMANENT_SES_ERRORS:
            return PERMANENT_SES_ERRORS[code], False
        if code in TRANSIENT_SES_ERRORS:
            return "SES_TRANSIENT", True
        if code in TRANSIENT_DYNAMODB_ERRORS:
            return "DYNAMODB_TRANSIENT", True
    # Unknown errors retry: a wasted retry is cheaper than a silently dropped email.
    return "UNKNOWN", True


def _record_error(key, request_id, status, error_code, exc):
    update = "SET #s = :status, error_code = :code, error_message = :msg"
    values = {":status": status, ":code": error_code, ":msg": str(exc)[:1000]}
    if status == "failed":
        update += ", failed_at = :now"
        values[":now"] = datetime.now(timezone.utc).strftime(TS_FORMAT)
    try:
        table.update_item(
            Key=key,
            UpdateExpression=update,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception(f"Failed to record send error | request_id={request_id} error_code={error_code}")


def _mark_sent(key, request_id, message_id):
    update = "SET #s = :sent, sent_at = :now"
    values = {":sent": "sent", ":now": datetime.now(timezone.utc).strftime(TS_FORMAT)}
    if message_id:
        update += ", ses_message_id = :mid"
        values[":mid"] = message_id
    try:
        table.update_item(
            Key=key,
            UpdateExpression=update + " REMOVE error_code, error_message",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except Exception:
        # Raising here would redeliver a message SES already accepted and send the email twice.
        logger.exception(f"Email sent but record not marked sent | request_id={request_id} ses_message_id={message_id}")


def _send_via_ses(from_email, to_email, subject, html_body, text_body, request_id, api_key_id, attachments, reply_to=None):
    if not attachments:
        body_payload = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
        if text_body:
            body_payload["Text"] = {"Data": text_body, "Charset": "UTF-8"}
        response = ses.send_email(
            Source=from_email,
            Destination={"ToAddresses": [to_email]},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body_payload},
            Tags=[{"Name": "request_id", "Value": request_id}, {"Name": "api_key_id", "Value": api_key_id}],
            **({"ReplyToAddresses": [reply_to]} if reply_to else {}),
        )
        return response["MessageId"]

    # sesv2 ignores ReplyToAddresses when Content is Raw, so the header goes into the MIME message itself.
    raw = _build_mime_message(from_email, to_email, subject, html_body, text_body, attachments, reply_to)
    response = sesv2.send_email(
        FromEmailAddress=from_email,
        Destination={"ToAddresses": [to_email]},
        Content={"Raw": {"Data": raw}},
        EmailTags=[{"Name": "request_id", "Value": request_id}, {"Name": "api_key_id", "Value": api_key_id}],
    )
    return response["MessageId"]


def _build_mime_message(from_email, to_email, subject, html_body, text_body, attachments, reply_to=None):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to

    body = MIMEMultipart("alternative")
    if text_body:
        body.attach(MIMEText(text_body, "plain", "utf-8"))
    body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(body)

    for attachment in attachments:
        obj = s3.get_object(Bucket=ATTACHMENTS_BUCKET, Key=attachment["object_key"])
        part = MIMEApplication(obj["Body"].read())
        part.add_header("Content-Disposition", "attachment", filename=attachment["filename"])
        msg.attach(part)

    return msg.as_bytes()


def _cleanup_attachments(attachments):
    for attachment in attachments or []:
        try:
            s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=attachment["object_key"])
        except Exception:
            logger.exception(f"Failed to delete attachment | object_key={attachment.get('object_key')}")

