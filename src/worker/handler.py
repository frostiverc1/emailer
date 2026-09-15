import json
import os
import logging
import smtplib
import base64
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode
import urllib.request
import boto3
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from jinja2 import Template, TemplateSyntaxError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "us-east-1"))
kms = boto3.client("kms")  # only used once the OAuth/SMTP paths are actually built

MAX_RECEIVE_COUNT = int(os.environ["MAX_RECEIVE_COUNT"])
LEASE_SECONDS = int(os.environ["LEASE_SECONDS"])
# Same fixed-width format validate writes, so lease timestamps compare correctly as strings.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

PERMANENT_SES_ERRORS = {
    "MessageRejected": "SES_REJECTED",
    "MailFromDomainNotVerifiedException": "SES_REJECTED",
    "ConfigurationSetDoesNotExist": "SES_REJECTED",
    "InvalidParameterValue": "SES_REJECTED",
    "AccountSendingPausedException": "SES_ACCOUNT_PAUSED",
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

# GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars are not needed for the pilot,
# only wire these up when _send_via_oauth_smtp actually gets used post-pilot
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


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
        if retryable:
            raise  # SQS redelivers, or moves the message to the DLQ on the last attempt
        return

    _mark_sent(key, request_id, message_id)


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
    for field in ("api_key", "service_id", "template_id", "template_params"):
        if field not in msg:
            raise PermanentError("INVALID_MESSAGE", f"Missing field: {field}")
    params = msg["template_params"]
    if not isinstance(params, dict) or not params.get("to_email"):
        raise PermanentError("INVALID_MESSAGE", "template_params.to_email is required")

    svc_id = msg["service_id"]
    tpl_id = msg["template_id"]
    request_id = msg["request_id"]

    # 1. fetch service config (full record, no projection now that provider_type branches the send path)
    svc = table.get_item(Key={"PK": f"SVC#{svc_id}", "SK": "META"})
    if "Item" not in svc:
        raise PermanentError("SERVICE_NOT_FOUND", f"Service not found: {svc_id}")

    svc_full = svc["Item"]

    # 2. fetch template
    tpl = table.get_item(
        Key={"PK": f"SVC#{svc_id}", "SK": f"TPL#{tpl_id}"},
        ProjectionExpression="subject_tpl, html_tpl, text_tpl",
    )
    if "Item" not in tpl:
        raise PermanentError("TEMPLATE_NOT_FOUND", f"Template not found: {tpl_id}")

    t = tpl["Item"]

    # 3. render with Jinja2
    subject = Template(t["subject_tpl"]).render(**params)
    html_body = Template(t["html_tpl"]).render(**params)
    text_body = Template(t["text_tpl"]).render(**params) if t.get("text_tpl") else None

    # 4. branch by provider
    provider = svc_full.get("provider_type", "ses")
    to_email = params["to_email"]

    if provider == "ses":
        if not svc_full.get("ses_from_email"):
            raise PermanentError("SERVICE_MISCONFIGURED", f"Service {svc_id} has no ses_from_email")
        message_id = _send_via_ses(svc_full["ses_from_email"], to_email, subject, html_body, text_body, request_id, msg["api_key"])
    elif provider in ("google", "outlook"):
        message_id = _send_via_oauth_smtp(svc_full, to_email, subject, html_body, text_body)
    elif provider == "smtp":
        message_id = _send_via_plain_smtp(svc_full, to_email, subject, html_body, text_body)
    else:
        raise PermanentError("SERVICE_MISCONFIGURED", f"Unknown provider_type: {provider}")

    logger.info(f"Sent email | request_id={request_id} to={to_email} provider={provider}")
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


def _send_via_ses(from_email, to_email, subject, html_body, text_body, request_id, api_key):
    body_payload = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
    if text_body:
        body_payload["Text"] = {"Data": text_body, "Charset": "UTF-8"}
    response = ses.send_email(
        Source=from_email,
        Destination={"ToAddresses": [to_email]},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body_payload},
        Tags=[{"Name": "request_id", "Value": request_id}, {"Name": "api_key", "Value": api_key}],
    )
    return response["MessageId"]


def _send_via_oauth_smtp(svc, to_email, subject, html_body, text_body):
    # Deferred, not part of the pilot build, see docs/email-service-pilot-lld.md section 4.
    refresh_token = kms.decrypt(CiphertextBlob=base64.b64decode(svc["encrypted_refresh_token"]))["Plaintext"].decode()
    access_token = _refresh_google_access_token(refresh_token)  # Outlook variant follows the same shape against Microsoft's token endpoint, deferred

    auth_string = f"user={svc['from_email']}\x01auth=Bearer {access_token}\x01\x01"
    auth_b64 = base64.b64encode(auth_string.encode()).decode()

    smtp_host = "smtp.gmail.com" if svc["provider_type"] == "google" else "smtp.office365.com"
    mime_msg = _build_mime_message(svc["from_email"], to_email, subject, html_body, text_body)

    with smtplib.SMTP(smtp_host, 587) as smtp:
        smtp.starttls()
        smtp.docmd("AUTH", "XOAUTH2 " + auth_b64)
        smtp.sendmail(svc["from_email"], [to_email], mime_msg.as_string())


def _send_via_plain_smtp(svc, to_email, subject, html_body, text_body):
    # Deferred, not part of the pilot build, see docs/email-service-pilot-lld.md section 4.
    password = kms.decrypt(CiphertextBlob=base64.b64decode(svc["encrypted_smtp_password"]))["Plaintext"].decode()
    mime_msg = _build_mime_message(svc["from_email"], to_email, subject, html_body, text_body)

    with smtplib.SMTP(svc["smtp_host"], int(svc["smtp_port"])) as smtp:
        smtp.starttls()
        smtp.login(svc["from_email"], password)
        smtp.sendmail(svc["from_email"], [to_email], mime_msg.as_string())


def _build_mime_message(from_email, to_email, subject, html_body, text_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    return msg


def _refresh_google_access_token(refresh_token):
    data = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]
