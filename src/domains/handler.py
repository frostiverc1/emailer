import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])
SES_REGION = os.environ.get("SES_REGION", "us-east-1")
sesv2 = boto3.client("sesv2", region_name=SES_REGION)

# The API routes sit behind the Cognito JWT authorizer; the caller's account comes from the token.

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# An unverified claim is released this long after it was first added, so nobody can hold a
# domain they can't verify. Retrying doesn't extend it.
CLAIM_TTL_SECONDS = 7 * 86400
# Subdomain used as the custom MAIL FROM (bounce) domain, e.g. bounce.acme.com.
MAIL_FROM_LABEL = "bounce"
DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Every domain has an entry in this partition, so the checker can find them without scanning the table.
DOMAIN_INDEX_PK = "DOMAINS"
# Settled domains (verified or failed) are re-checked daily to catch customers removing their records.
SETTLED_CHECK_INTERVAL_SECONDS = 86400
# "Check now" hits SES at most this often per domain; the SES API quota is shared by every customer.
CHECK_NOW_MIN_INTERVAL_SECONDS = 10
# The scheduled run stops with this much Lambda time left; unchecked domains are picked up next run.
TIME_BUDGET_MS = 30_000


class MissingHostedZone(Exception):
    pass


def handler(event, context):
    # EventBridge Scheduler invokes the checker with this fixed input.
    if event.get("action") == "check_domains":
        return _check_domains(context)

    # Same account id the admin Lambda derives, one account per Cognito user.
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        return _resp(401, {"error": "Unauthorized"})
    account_id = f"acct_{claims['sub']}"

    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON"})

    if route == "POST /admin/domains":
        return _add_domain(account_id, body)
    if route == "GET /admin/domains":
        return _list_domains(account_id)
    if route == "GET /admin/domains/{domain}":
        return _get_domain(params.get("domain"), account_id)
    if route == "DELETE /admin/domains/{domain}":
        return _delete_domain(params.get("domain"), account_id)
    if route == "POST /admin/domains/{domain}/check":
        return _check_domain(params.get("domain"), account_id)
    if route == "POST /admin/domains/{domain}/retry":
        return _retry_domain(params.get("domain"), account_id)

    return _resp(404, {"error": "Unknown route"})


def _add_domain(account_id, body):
    domain, error = _normalize_domain(body.get("domain"))
    if error:
        return _resp(400, {"error": error})

    conflict = _find_conflict(domain, account_id)
    if conflict:
        return _resp(409, {"error": conflict})

    # Claim before touching SES: if two accounts race for a domain only one claim succeeds,
    # and the loser never creates or deletes an SES identity that belongs to the winner.
    now = _now()
    try:
        table.put_item(
            Item={
                "PK": f"DOMAIN#{domain}",
                "SK": "META",
                "domain": domain,
                "account_id": account_id,
                "status": "creating",
                "created_at": now,
                "updated_at": now,
                "claim_expires_at": int(time.time()) + CLAIM_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resp(409, {"error": f"{domain} has already been added"})
        raise
    table.put_item(Item={"PK": f"ACCT#{account_id}", "SK": f"DOMAIN#{domain}", "domain": domain, "created_at": now})
    table.put_item(Item={"PK": DOMAIN_INDEX_PK, "SK": f"DOMAIN#{domain}", "domain": domain})

    mail_from_domain = f"{MAIL_FROM_LABEL}.{domain}"
    identity_created = False
    try:
        dkim = _create_identity(domain)
        identity_created = True
        _put_mail_from(domain)
    except Exception as exc:
        # MissingHostedZone is raised after SES created the identity, so it must be deleted too.
        _release_claim(domain, account_id, delete_identity=identity_created or isinstance(exc, MissingHostedZone))
        if isinstance(exc, ClientError):
            code = exc.response["Error"]["Code"]
            if code == "AlreadyExistsException":
                # No claim existed, so this identity was made outside this app. Leave it alone.
                return _resp(409, {"error": f"{domain} already exists in SES outside this app"})
            logger.error(f"SES error adding domain | domain={domain} code={code} error={exc}")
            return _resp(502, {"error": f"SES error: {code}"})
        if isinstance(exc, MissingHostedZone):
            logger.error(f"Cannot build DKIM records | domain={domain} error={exc}")
            return _resp(500, {"error": str(exc)})
        raise

    dkim_status = dkim.get("Status", "PENDING")
    item = table.update_item(
        Key={"PK": f"DOMAIN#{domain}", "SK": "META"},
        UpdateExpression=(
            "SET #s = :s, dkim_status = :ds, dkim_tokens = :t, signing_hosted_zone = :z, "
            "mail_from_domain = :m, mail_from_status = :ms, last_checked_at = :lc, updated_at = :u"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": _overall_status(dkim_status),
            ":ds": dkim_status,
            ":t": dkim.get("Tokens", []),
            ":z": dkim["SigningHostedZone"],
            ":m": mail_from_domain,
            ":ms": "PENDING",
            ":lc": int(time.time()),
            ":u": _now(),
        },
        ReturnValues="ALL_NEW",
    )["Attributes"]
    logger.info(f"Domain added | domain={domain} account_id={account_id}")
    return _resp(201, _domain_view(item))


def _list_domains(account_id):
    domains = []
    for entry in _query_all(Key("PK").eq(f"ACCT#{account_id}") & Key("SK").begins_with("DOMAIN#")):
        item = table.get_item(Key={"PK": f"DOMAIN#{entry['domain']}", "SK": "META"}).get("Item")
        if item and item["account_id"] == account_id:
            domains.append(_domain_summary(item))
    return _resp(200, {"domains": domains})


def _get_domain(raw_domain, account_id):
    item, error = _owned_domain(raw_domain, account_id)
    if error:
        return error
    return _resp(200, _domain_view(item))


def _delete_domain(raw_domain, account_id):
    item, error = _owned_domain(raw_domain, account_id)
    if error:
        return error

    domain = item["domain"]
    try:
        sesv2.delete_email_identity(EmailIdentity=domain)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code != "NotFoundException":
            logger.error(f"SES error deleting domain | domain={domain} code={code} error={exc}")
            return _resp(502, {"error": f"SES error: {code}"})

    _delete_domain_items(domain, item["account_id"])
    logger.info(f"Domain deleted | domain={domain} account_id={item['account_id']}")
    return _resp(204, None)


def _check_domain(raw_domain, account_id):
    item, error = _owned_domain(raw_domain, account_id)
    if error:
        return error
    if item.get("status") == "creating":
        return _resp(409, {"error": "Domain is still being added"})

    if int(time.time()) - int(item.get("last_checked_at", 0)) >= CHECK_NOW_MIN_INTERVAL_SECONDS:
        try:
            item = _refresh(item)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            logger.error(f"SES error checking domain | domain={raw_domain} code={code} error={exc}")
            return _resp(502, {"error": f"SES error: {code}"})
        if item is None:
            return _resp(404, {"error": "Domain not found"})
    return _resp(200, _domain_view(item))


def _retry_domain(raw_domain, account_id):
    item, error = _owned_domain(raw_domain, account_id)
    if error:
        return error

    domain = item["domain"]
    dkim_failed = item.get("status") == "failed"
    if not dkim_failed and item.get("mail_from_status") != "FAILED":
        return _resp(409, {"error": "Nothing to retry, verification hasn't failed"})

    failure = None
    try:
        if dkim_failed:
            # SES stops looking for DKIM records after 72 hours, and a revoked setup must be started
            # over. Recreating the identity restarts it, and SES may issue new DKIM tokens, so the
            # refresh below re-reads the records the customer has to publish.
            try:
                sesv2.delete_email_identity(EmailIdentity=domain)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "NotFoundException":
                    raise
            _create_identity(domain)
        # Also restarts a failed MAIL FROM check, and restores it after the identity was recreated.
        _put_mail_from(domain)
    except (ClientError, MissingHostedZone) as exc:
        failure = exc
        logger.error(f"Retry failed | domain={domain} error={exc}")

    # Sync from SES either way: a half-finished retry must show its real state, not the old one.
    try:
        item = _refresh(item) or item
    except ClientError:
        logger.exception(f"Failed to refresh after retry | domain={domain}")

    if isinstance(failure, ClientError):
        return _resp(502, {"error": f"SES error: {failure.response['Error']['Code']}"})
    if isinstance(failure, MissingHostedZone):
        return _resp(500, {"error": str(failure)})
    logger.info(f"Domain verification restarted | domain={domain} dkim={dkim_failed}")
    return _resp(200, _domain_view(item))


def _check_domains(context):
    """Scheduled run: refresh statuses, and release claims that should no longer be held."""
    now = int(time.time())
    items = _all_domains()
    verified_owners = {i["domain"]: i["account_id"] for i in items if i.get("status") == "verified"}
    result = {"domains": len(items), "checked": 0, "released": 0, "stopped_early": False}

    for item in items:
        if context is not None and context.get_remaining_time_in_millis() < TIME_BUDGET_MS:
            result["stopped_early"] = True
            break

        domain, account_id = item["domain"], item["account_id"]
        # "creating" means an add is in progress, or it crashed partway and waits for its claim to expire.
        if item.get("status") == "creating":
            if now >= int(item.get("claim_expires_at", 0)):
                result["released"] += _release_claim(domain, account_id, delete_identity=True, reason="add never finished")
            continue

        release_reason = None
        if "verified_at" not in item:
            parent = _verified_parent_of_other_account(domain, account_id, verified_owners)
            if parent:
                # The add-time conflict check blocks new claims like this, but a claim added before the
                # parent was verified would otherwise keep overriding the parent's settings.
                release_reason = f"subdomain of {parent}, verified by another account"
            elif now >= int(item.get("claim_expires_at", 0)):
                release_reason = "claim expired unverified"
        if not release_reason and not _due_for_check(item, now):
            continue

        try:
            refreshed = _refresh(item)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TooManyRequestsException":
                logger.warning("SES throttled the domain check, stopping until the next run")
                result["stopped_early"] = True
                break
            # Never release on an unknown state; the next run tries again.
            logger.exception(f"Failed to check domain | domain={domain}")
            continue
        if refreshed is None:
            continue
        result["checked"] += 1
        # Decided after refreshing, so a domain whose records went live since the last run is kept.
        if release_reason and "verified_at" not in refreshed:
            result["released"] += _release_claim(domain, account_id, delete_identity=True, reason=release_reason)

    logger.info(f"Domain check finished | {json.dumps(result)}")
    return result


def _due_for_check(item, now):
    still_changing = item.get("status") == "pending" or item.get("mail_from_status") in ("PENDING", "TEMPORARY_FAILURE")
    interval = 0 if still_changing else SETTLED_CHECK_INTERVAL_SECONDS
    return now - int(item.get("last_checked_at", 0)) >= interval


def _refresh(item):
    """Copy the identity's current state from SES onto the domain record. Returns None if the record is gone."""
    domain = item["domain"]
    try:
        identity = sesv2.get_email_identity(EmailIdentity=domain)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NotFoundException":
            raise
        identity = None

    if identity is None:
        # Deleted outside this app, or a retry recreated nothing. Retry creates it again.
        dkim_status, mail_from_status = "MISSING", None
        tokens, zone = item.get("dkim_tokens", []), item.get("signing_hosted_zone")
    else:
        dkim = identity.get("DkimAttributes", {})
        dkim_status = dkim.get("Status", "NOT_STARTED")
        mail_from_status = identity.get("MailFromAttributes", {}).get("MailFromDomainStatus")
        tokens = dkim.get("Tokens") or item.get("dkim_tokens", [])
        zone = dkim.get("SigningHostedZone") or item.get("signing_hosted_zone")

    was_verified = "verified_at" in item
    status = _overall_status(dkim_status, was_verified)
    update = (
        "SET #s = :s, dkim_status = :ds, dkim_tokens = :t, signing_hosted_zone = :z, "
        "mail_from_status = :ms, last_checked_at = :lc, updated_at = :u"
    )
    values = {
        ":s": status,
        ":ds": dkim_status,
        ":t": tokens,
        ":z": zone,
        ":ms": mail_from_status,
        ":lc": int(time.time()),
        ":u": _now(),
    }
    if status == "verified" and not was_verified:
        update += ", verified_at = :va REMOVE claim_expires_at"
        values[":va"] = values[":u"]

    try:
        updated = table.update_item(
            Key={"PK": f"DOMAIN#{domain}", "SK": "META"},
            UpdateExpression=update,
            # Never recreate a record that was deleted while SES was being asked.
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise

    if status != item.get("status"):
        logger.info(f"Domain status changed | domain={domain} from={item.get('status')} to={status} dkim={dkim_status}")
    return updated


def _owned_domain(raw_domain, account_id):
    domain, error = _normalize_domain(raw_domain)
    if error:
        return None, _resp(400, {"error": error})
    item = table.get_item(Key={"PK": f"DOMAIN#{domain}", "SK": "META"}).get("Item")
    # Same 404 for "missing" and "someone else's", so accounts can't probe other accounts' domains.
    if not item or item["account_id"] != account_id:
        return None, _resp(404, {"error": "Domain not found"})
    return item, None


def _normalize_domain(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None, "domain is required"
    domain = raw.strip().lower().rstrip(".")
    if not domain.isascii():
        return None, "Internationalised domains must be entered in punycode (xn--) form"
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(DNS_LABEL.match(label) for label in labels):
        return None, f"Not a valid domain: {raw}"
    if labels[-1].isdigit():
        return None, f"Not a valid domain: {raw}"
    return domain, None


def _find_conflict(domain, account_id):
    existing = table.get_item(Key={"PK": f"DOMAIN#{domain}", "SK": "META"}).get("Item")
    if existing:
        if existing["account_id"] == account_id:
            return f"{domain} is already added to this account"
        return f"{domain} is already claimed by another account"

    # A verified parent already lets SES send as all of its subdomains, and a separate subdomain
    # identity would override the parent's DKIM settings for that subdomain. So only the parent's
    # owner may add its subdomains. Unverified parents don't block: they can't send anything, and
    # counting them would let a bogus claim on e.g. "co.uk" block every domain under it.
    for parent in _parents(domain):
        item = table.get_item(Key={"PK": f"DOMAIN#{parent}", "SK": "META"}).get("Item")
        if item and item["account_id"] != account_id and item.get("status") == "verified":
            return f"{domain} is a subdomain of {parent}, which another account has verified"
    return None


def _verified_parent_of_other_account(domain, account_id, verified_owners):
    for parent in _parents(domain):
        owner = verified_owners.get(parent)
        if owner and owner != account_id:
            return parent
    return None


def _parents(domain):
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(1, len(labels) - 1)]


def _create_identity(domain):
    # Easy DKIM with SES's default 2048-bit key.
    dkim = sesv2.create_email_identity(EmailIdentity=domain).get("DkimAttributes", {})
    # The CNAME target differs by region and identity. Older SDKs drop this field, and guessing
    # it would show customers DNS records that never verify.
    if not dkim.get("SigningHostedZone"):
        raise MissingHostedZone("SES response has no DkimAttributes.SigningHostedZone, the Lambda's boto3 is too old")
    return dkim


def _put_mail_from(domain):
    # USE_DEFAULT_VALUE keeps sending working while the bounce MX record is missing.
    sesv2.put_email_identity_mail_from_attributes(
        EmailIdentity=domain,
        MailFromDomain=f"{MAIL_FROM_LABEL}.{domain}",
        BehaviorOnMxFailure="USE_DEFAULT_VALUE",
    )


def _release_claim(domain, account_id, delete_identity, reason="rollback"):
    """Best effort. Returns 1 if the claim was released, 0 if not, so callers can count."""
    # SES first: if that fails the claim stays, so the next run can try again. The other order
    # could strand an identity with no claim, which would block the real owner from adding it.
    if delete_identity:
        try:
            sesv2.delete_email_identity(EmailIdentity=domain)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NotFoundException":
                logger.exception(f"Failed to delete SES identity while releasing claim | domain={domain} reason={reason}")
                return 0
    try:
        _delete_domain_items(domain, account_id)
    except ClientError:
        logger.exception(f"Failed to release domain claim | domain={domain} reason={reason}")
        return 0
    logger.info(f"Released domain claim | domain={domain} account_id={account_id} reason={reason}")
    return 1


def _delete_domain_items(domain, account_id):
    table.delete_item(Key={"PK": f"DOMAIN#{domain}", "SK": "META"})
    table.delete_item(Key={"PK": f"ACCT#{account_id}", "SK": f"DOMAIN#{domain}"})
    table.delete_item(Key={"PK": DOMAIN_INDEX_PK, "SK": f"DOMAIN#{domain}"})


def _all_domains():
    items = []
    for entry in _query_all(Key("PK").eq(DOMAIN_INDEX_PK)):
        item = table.get_item(Key={"PK": f"DOMAIN#{entry['domain']}", "SK": "META"}).get("Item")
        if item:
            items.append(item)
        else:
            table.delete_item(Key={"PK": DOMAIN_INDEX_PK, "SK": entry["SK"]})
    return items


def _query_all(key_condition):
    items = []
    kwargs = {"KeyConditionExpression": key_condition}
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _overall_status(dkim_status, was_verified=False):
    # Verified means DKIM succeeded for this exact domain. SES's own "verified for sending" flag is
    # not used, because a subdomain inherits it from a verified parent without proving anything.
    if dkim_status == "SUCCESS":
        return "verified"
    if dkim_status in ("FAILED", "MISSING"):
        return "failed"
    # TEMPORARY_FAILURE means SES can't currently check the records. A domain that already proved
    # ownership keeps its status, so a DNS hiccup doesn't block the customer's sending; SES moves
    # it to FAILED if the records are really gone.
    if dkim_status == "TEMPORARY_FAILURE" and was_verified:
        return "verified"
    return "pending"


def _epoch(value):
    return int(value) if value is not None else None


def _domain_summary(item):
    return {
        "domain": item["domain"],
        "status": item.get("status"),
        "dkim_status": item.get("dkim_status"),
        "mail_from_status": item.get("mail_from_status"),
        "created_at": item.get("created_at"),
        "verified_at": item.get("verified_at"),
        "last_checked_at": _epoch(item.get("last_checked_at")),
        "claim_expires_at": _epoch(item.get("claim_expires_at")),
    }


def _domain_view(item):
    domain = item["domain"]
    zone = item.get("signing_hosted_zone")
    records = [{
        "type": "CNAME",
        "name": f"{token}._domainkey.{domain}",
        "value": f"{token}.{zone}",
        "required": True,
        "purpose": "DKIM: proves you own the domain and signs your emails",
    } for token in item.get("dkim_tokens", [])]

    mail_from = item.get("mail_from_domain")
    if mail_from:
        records.append({
            "type": "MX",
            "name": mail_from,
            "value": f"feedback-smtp.{SES_REGION}.amazonses.com",
            "priority": 10,
            "required": False,
            "purpose": "Bounce subdomain: bounce reports come back through your domain. Must be the only MX record on this name.",
        })
        records.append({
            "type": "TXT",
            "name": mail_from,
            "value": '"v=spf1 include:amazonses.com ~all"',
            "required": False,
            "purpose": "SPF for the bounce subdomain",
        })
    records.append({
        "type": "TXT",
        "name": f"_dmarc.{domain}",
        "value": "v=DMARC1; p=none;",
        "required": False,
        "purpose": "DMARC: tells inboxes how to treat unauthenticated mail. Skip if the domain already has a _dmarc record, a domain can only have one.",
    })

    return {
        **_domain_summary(item),
        "account_id": item["account_id"],
        "mail_from_domain": mail_from,
        "updated_at": item.get("updated_at"),
        "records": records,
    }


def _now():
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "",
    }
