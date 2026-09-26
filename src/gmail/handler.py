import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])

CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
REDIRECT_URI = os.environ["GOOGLE_OAUTH_REDIRECT_URI"]
# Where the browser lands after the callback finishes, success or not (the dashboard's Gmail settings page).
FRONTEND_RETURN_URL = os.environ["GMAIL_FRONTEND_RETURN_URL"]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
# gmail.send: this app never reads, lists or modifies the customer's mail. email: just their address,
# to show and check against — gmail.send alone can't call Gmail's own profile endpoint to learn it
# (that needs a broader, more sensitive scope), so this uses Google's separate, non-sensitive userinfo API instead.
SCOPE = "https://www.googleapis.com/auth/gmail.send email"
# The state row is a short-lived, one-time CSRF token binding the callback back to the account
# that started it (Google's redirect carries no auth of its own).
STATE_TTL_SECONDS = 10 * 60


def handler(event, context):
    route = f"{event.get('httpMethod')} {event.get('resource')}"
    qs = event.get("queryStringParameters") or {}

    # The callback is hit by Google's redirect, not the logged-in browser session, so it carries
    # no Cognito token. Every other route sits behind Cognito (infra/modules/api/main.tf).
    if route == "GET /admin/gmail/callback":
        return _callback(qs)

    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    if not claims.get("sub"):
        return _resp(401, {"error": "Unauthorized"})
    account_id = f"acct_{claims['sub']}"

    if route == "GET /admin/gmail/connect":
        return _connect(account_id)
    if route == "GET /admin/gmail":
        return _status(account_id)
    if route == "DELETE /admin/gmail":
        return _disconnect(account_id)

    return _resp(404, {"error": "Unknown route"})


def _gmail_key(account_id):
    return {"PK": f"ACCT#{account_id}", "SK": "GMAIL"}


def _connect(account_id):
    state = uuid.uuid4().hex
    table.put_item(Item={
        "PK": f"GMAILSTATE#{state}",
        "SK": "META",
        "account_id": account_id,
        "ttl": int(time.time()) + STATE_TTL_SECONDS,
    })
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Forces Google to reissue a refresh token even on a reconnect (it's only sent on first
        # consent otherwise), so an account can disconnect and reconnect, or connect a new address.
        "prompt": "consent",
        "state": state,
    }
    return _resp(200, {"auth_url": f"{AUTH_URL}?{urllib.parse.urlencode(params)}"})


def _callback(qs):
    error = qs.get("error")
    state = qs.get("state")
    code = qs.get("code")

    account_id = None
    if state:
        row = table.get_item(Key={"PK": f"GMAILSTATE#{state}", "SK": "META"}).get("Item")
        if row:
            # One-time use: whether this succeeds or fails below, the state can't be replayed.
            table.delete_item(Key={"PK": f"GMAILSTATE#{state}", "SK": "META"})
            account_id = row.get("account_id")

    if error or not account_id or not code:
        logger.warning(f"Gmail connect failed | error={error} has_account={bool(account_id)} has_code={bool(code)}")
        return _redirect_result(False)

    try:
        tokens = _exchange_code(code)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            # Google omits this when the account already granted consent and "prompt=consent"
            # wasn't honored (shouldn't happen since _connect always sets it, but don't store nothing).
            raise RuntimeError("No refresh_token in Google's response")
        connected_email = _fetch_userinfo_email(tokens["access_token"])
    except Exception:
        logger.exception(f"Gmail token exchange failed | account_id={account_id}")
        return _redirect_result(False)

    table.put_item(Item={
        **_gmail_key(account_id),
        "connected_email": connected_email,
        "refresh_token": refresh_token,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info(f"Gmail connected | account_id={account_id} email={connected_email}")
    return _redirect_result(True)


def _redirect_result(success):
    url = f"{FRONTEND_RETURN_URL}?gmail_connected={'1' if success else '0'}"
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}


def _status(account_id):
    item = table.get_item(Key=_gmail_key(account_id)).get("Item")
    if not item:
        return _resp(200, {"connected": False})
    return _resp(200, {
        "connected": True,
        "email": item.get("connected_email"),
        "connected_at": item.get("connected_at"),
    })


def _disconnect(account_id):
    item = table.get_item(Key=_gmail_key(account_id)).get("Item")
    if not item:
        return _resp(204, None)

    try:
        _post_form(REVOKE_URL, {"token": item["refresh_token"]})
    except Exception:
        # Best effort: Google's side is just cleanup. Deleting our copy is what actually stops sending.
        logger.exception(f"Failed to revoke Gmail token with Google | account_id={account_id}")

    table.delete_item(Key=_gmail_key(account_id))
    logger.info(f"Gmail disconnected | account_id={account_id}")
    return _resp(204, None)


def _exchange_code(code):
    return _post_form(TOKEN_URL, {
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })


def _fetch_userinfo_email(access_token):
    req = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["email"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{USERINFO_URL} returned {exc.code}: {body}") from exc


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} returned {exc.code}: {body}") from exc


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body) if body is not None else "",
    }
