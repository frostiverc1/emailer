import json
import os
import logging
import smtplib
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode
import urllib.request
import boto3
from jinja2 import Template

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "us-east-1"))
kms = boto3.client("kms")  # only used once the OAuth/SMTP paths are actually built

# GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars are not needed for the pilot,
# only wire these up when _send_via_oauth_smtp actually gets used post-pilot
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def handler(event, context):
    for record in event["Records"]:
        msg = json.loads(record["body"])
        _process(msg)


def _process(msg):
    svc_id = msg["service_id"]
    tpl_id = msg["template_id"]
    params = msg["template_params"]
    request_id = msg["request_id"]

    # 1. fetch service config (full record, no projection now that provider_type branches the send path)
    svc = table.get_item(Key={"PK": f"SVC#{svc_id}", "SK": "META"})
    if "Item" not in svc:
        logger.error(f"Service not found: {svc_id} | request_id={request_id}")
        raise ValueError(f"Service not found: {svc_id}")

    svc_full = svc["Item"]

    # 2. fetch template
    tpl = table.get_item(
        Key={"PK": f"SVC#{svc_id}", "SK": f"TPL#{tpl_id}"},
        ProjectionExpression="subject_tpl, html_tpl, text_tpl",
    )
    if "Item" not in tpl:
        logger.error(f"Template not found: {tpl_id} | request_id={request_id}")
        raise ValueError(f"Template not found: {tpl_id}")

    t = tpl["Item"]

    # 3. render with Jinja2
    subject = Template(t["subject_tpl"]).render(**params)
    html_body = Template(t["html_tpl"]).render(**params)
    text_body = Template(t["text_tpl"]).render(**params) if t.get("text_tpl") else None

    # 4. branch by provider
    provider = svc_full.get("provider_type", "ses")
    to_email = params["to_email"]

    if provider == "ses":
        _send_via_ses(svc_full["ses_from_email"], to_email, subject, html_body, text_body, request_id, msg["api_key"])
    elif provider in ("google", "outlook"):
        _send_via_oauth_smtp(svc_full, to_email, subject, html_body, text_body)
    elif provider == "smtp":
        _send_via_plain_smtp(svc_full, to_email, subject, html_body, text_body)
    else:
        raise ValueError(f"Unknown provider_type: {provider}")

    logger.info(f"Sent email | request_id={request_id} to={to_email} provider={provider}")


def _send_via_ses(from_email, to_email, subject, html_body, text_body, request_id, api_key):
    body_payload = {"Html": {"Data": html_body, "Charset": "UTF-8"}}
    if text_body:
        body_payload["Text"] = {"Data": text_body, "Charset": "UTF-8"}
    ses.send_email(
        Source=from_email,
        Destination={"ToAddresses": [to_email]},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body_payload},
        Tags=[{"Name": "request_id", "Value": request_id}, {"Name": "api_key", "Value": api_key}],
    )


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
