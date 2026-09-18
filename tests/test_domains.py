import json

import pytest
from botocore.exceptions import ClientError

from conftest import load_handler

ZONE = "dkim.amazonses.com"
TOKENS = ["tok1", "tok2", "tok3"]


@pytest.fixture
def h(table):
    module = load_handler("domains", table)
    module.sesv2.create_email_identity.return_value = {
        "IdentityType": "DOMAIN",
        "VerifiedForSendingStatus": False,
        "DkimAttributes": {"Status": "PENDING", "Tokens": TOKENS, "SigningHostedZone": ZONE},
    }
    return module


def call(h, route, body=None, domain=None, account_id="acct_a"):
    event = {"routeKey": route}
    if body is not None:
        event["body"] = json.dumps(body)
    if domain is not None:
        event["pathParameters"] = {"domain": domain}
    if account_id is not None:
        # What the Cognito JWT authorizer passes on; the handler turns sub "a" into "acct_a".
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": account_id.removeprefix("acct_")}}}}
    resp = h.handler(event, None)
    return resp["statusCode"], (json.loads(resp["body"]) if resp["body"] else None)


def add(h, domain, account_id="acct_a"):
    return call(h, "POST /admin/domains", {"domain": domain}, account_id=account_id)


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def seed_domain(table, domain, account_id, status, **fields):
    table.put_item(Item={"PK": f"DOMAIN#{domain}", "SK": "META", "domain": domain, "account_id": account_id, "status": status, **fields})
    table.put_item(Item={"PK": f"ACCT#{account_id}", "SK": f"DOMAIN#{domain}", "domain": domain})
    table.put_item(Item={"PK": "DOMAINS", "SK": f"DOMAIN#{domain}", "domain": domain})


def claim_exists(table, domain, account_id):
    """(domain record, account index entry, all-domains index entry)"""
    return (
        "Item" in table.get_item(Key={"PK": f"DOMAIN#{domain}", "SK": "META"}),
        "Item" in table.get_item(Key={"PK": f"ACCT#{account_id}", "SK": f"DOMAIN#{domain}"}),
        "Item" in table.get_item(Key={"PK": "DOMAINS", "SK": f"DOMAIN#{domain}"}),
    )


# --- adding a domain ---

def test_add_domain_returns_dns_records_and_saves_claim(h, table):
    status, body = add(h, "acme.com")

    assert status == 201
    assert body["domain"] == "acme.com"
    assert body["account_id"] == "acct_a"
    assert body["status"] == "pending"
    assert body["dkim_status"] == "PENDING"
    assert body["mail_from_domain"] == "bounce.acme.com"

    records = [(r["type"], r["name"], r["value"]) for r in body["records"]]
    assert records == [
        ("CNAME", "tok1._domainkey.acme.com", "tok1.dkim.amazonses.com"),
        ("CNAME", "tok2._domainkey.acme.com", "tok2.dkim.amazonses.com"),
        ("CNAME", "tok3._domainkey.acme.com", "tok3.dkim.amazonses.com"),
        ("MX", "bounce.acme.com", "feedback-smtp.us-east-1.amazonses.com"),
        ("TXT", "bounce.acme.com", '"v=spf1 include:amazonses.com ~all"'),
        ("TXT", "_dmarc.acme.com", "v=DMARC1; p=none;"),
    ]
    assert [r["required"] for r in body["records"]] == [True, True, True, False, False, False]
    assert body["records"][3]["priority"] == 10

    h.sesv2.create_email_identity.assert_called_once_with(EmailIdentity="acme.com")
    h.sesv2.put_email_identity_mail_from_attributes.assert_called_once_with(
        EmailIdentity="acme.com", MailFromDomain="bounce.acme.com", BehaviorOnMxFailure="USE_DEFAULT_VALUE"
    )
    item = table.get_item(Key={"PK": "DOMAIN#acme.com", "SK": "META"})["Item"]
    assert item["account_id"] == "acct_a"
    assert item["dkim_tokens"] == TOKENS
    assert item["claim_expires_at"] > 0
    assert claim_exists(table, "acme.com", "acct_a") == (True, True, True)


def test_domain_is_normalised(h):
    status, body = add(h, "  ACME.com. ")
    assert status == 201
    assert body["domain"] == "acme.com"
    h.sesv2.create_email_identity.assert_called_once_with(EmailIdentity="acme.com")


@pytest.mark.parametrize("bad", [
    "", "   ", None, 42, "localhost", "http://acme.com", "hello@acme.com", "acme.com/path", "acme.com:8080",
    "1.2.3.4", "-acme.com", "acme-.com", "ac_me.com", "acmé.com", "a..com", "a" * 64 + ".com",
])
def test_invalid_domains_are_rejected(h, bad):
    status, _ = add(h, bad)
    assert status == 400
    h.sesv2.create_email_identity.assert_not_called()


def test_not_logged_in(h):
    status, body = call(h, "POST /admin/domains", {"domain": "acme.com"}, account_id=None)
    assert (status, body["error"]) == (401, "Unauthorized")
    h.sesv2.create_email_identity.assert_not_called()


def test_account_id_in_body_is_ignored(h, table):
    assert call(h, "POST /admin/domains", {"domain": "acme.com", "account_id": "acct_b"})[0] == 201
    assert table.get_item(Key={"PK": "DOMAIN#acme.com", "SK": "META"})["Item"]["account_id"] == "acct_a"


# --- ownership conflicts ---

def test_same_domain_same_account(h, table):
    seed_domain(table, "acme.com", "acct_a", "pending")
    status, body = add(h, "acme.com")
    assert (status, body["error"]) == (409, "acme.com is already added to this account")
    h.sesv2.create_email_identity.assert_not_called()


def test_same_domain_other_account(h, table):
    seed_domain(table, "acme.com", "acct_a", "pending")
    status, body = add(h, "acme.com", account_id="acct_b")
    assert (status, body["error"]) == (409, "acme.com is already claimed by another account")
    h.sesv2.create_email_identity.assert_not_called()


def test_subdomain_of_other_accounts_verified_domain_is_blocked(h, table):
    seed_domain(table, "acme.com", "acct_a", "verified")
    status, body = add(h, "mail.news.acme.com", account_id="acct_b")
    assert status == 409
    assert body["error"] == "mail.news.acme.com is a subdomain of acme.com, which another account has verified"
    h.sesv2.create_email_identity.assert_not_called()


def test_subdomain_of_own_verified_domain_is_allowed(h, table):
    seed_domain(table, "acme.com", "acct_a", "verified")
    assert add(h, "mail.acme.com")[0] == 201


def test_subdomain_of_other_accounts_unverified_domain_is_allowed(h, table):
    # e.g. a bogus claim on a shared suffix must not block everyone under it
    seed_domain(table, "co.uk", "acct_a", "pending")
    assert add(h, "acme.co.uk", account_id="acct_b")[0] == 201


def test_claim_race_lost_never_touches_ses(h, table, monkeypatch):
    # Simulate another account claiming the domain between the conflict check and the claim write.
    seed_domain(table, "acme.com", "acct_a", "creating")
    monkeypatch.setattr(h, "_find_conflict", lambda domain, account_id: None)
    status, body = add(h, "acme.com", account_id="acct_b")
    assert (status, body["error"]) == (409, "acme.com has already been added")
    h.sesv2.create_email_identity.assert_not_called()
    h.sesv2.delete_email_identity.assert_not_called()
    assert table.get_item(Key={"PK": "DOMAIN#acme.com", "SK": "META"})["Item"]["account_id"] == "acct_a"


# --- SES failures roll the claim back ---

def test_identity_already_in_ses_outside_app_is_left_alone(h, table):
    h.sesv2.create_email_identity.side_effect = client_error("AlreadyExistsException")
    status, body = add(h, "acme.com")
    assert (status, body["error"]) == (409, "acme.com already exists in SES outside this app")
    h.sesv2.delete_email_identity.assert_not_called()
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_ses_create_error_releases_claim(h, table):
    h.sesv2.create_email_identity.side_effect = client_error("LimitExceededException")
    status, body = add(h, "acme.com")
    assert (status, body["error"]) == (502, "SES error: LimitExceededException")
    h.sesv2.delete_email_identity.assert_not_called()
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_mail_from_error_deletes_identity_and_releases_claim(h, table):
    h.sesv2.put_email_identity_mail_from_attributes.side_effect = client_error("BadRequestException")
    status, _ = add(h, "acme.com")
    assert status == 502
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="acme.com")
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_missing_hosted_zone_fails_instead_of_guessing_records(h, table):
    h.sesv2.create_email_identity.return_value = {"DkimAttributes": {"Status": "PENDING", "Tokens": TOKENS}}
    status, body = add(h, "acme.com")
    assert status == 500
    assert "SigningHostedZone" in body["error"]
    h.sesv2.put_email_identity_mail_from_attributes.assert_not_called()
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="acme.com")
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_domain_can_be_added_again_after_rollback(h):
    h.sesv2.create_email_identity.side_effect = [client_error("TooManyRequestsException"), h.sesv2.create_email_identity.return_value]
    assert add(h, "acme.com")[0] == 502
    assert add(h, "acme.com")[0] == 201


# --- listing and viewing ---

def test_list_only_shows_own_domains(h):
    add(h, "acme.com")
    add(h, "acme.org")
    add(h, "other.com", account_id="acct_b")

    status, body = call(h, "GET /admin/domains", account_id="acct_a")
    assert status == 200
    assert sorted(d["domain"] for d in body["domains"]) == ["acme.com", "acme.org"]
    assert all(d["status"] == "pending" for d in body["domains"])
    assert "records" not in body["domains"][0]


def test_get_own_domain_includes_records(h):
    add(h, "acme.com")
    status, body = call(h, "GET /admin/domains/{domain}", domain="ACME.com", account_id="acct_a")
    assert status == 200
    assert body["domain"] == "acme.com"
    assert len(body["records"]) == 6


def test_get_other_accounts_domain_looks_missing(h):
    add(h, "acme.com")
    status, body = call(h, "GET /admin/domains/{domain}", domain="acme.com", account_id="acct_b")
    assert (status, body["error"]) == (404, "Domain not found")


# --- deleting ---

def test_delete_own_domain(h, table):
    add(h, "acme.com")
    status, body = call(h, "DELETE /admin/domains/{domain}", domain="acme.com", account_id="acct_a")
    assert (status, body) == (204, None)
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="acme.com")
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_delete_other_accounts_domain_is_refused(h, table):
    add(h, "acme.com")
    status, _ = call(h, "DELETE /admin/domains/{domain}", domain="acme.com", account_id="acct_b")
    assert status == 404
    h.sesv2.delete_email_identity.assert_not_called()
    assert claim_exists(table, "acme.com", "acct_a") == (True, True, True)


def test_delete_when_identity_already_gone_from_ses(h, table):
    add(h, "acme.com")
    h.sesv2.delete_email_identity.side_effect = client_error("NotFoundException")
    assert call(h, "DELETE /admin/domains/{domain}", domain="acme.com", account_id="acct_a")[0] == 204
    assert claim_exists(table, "acme.com", "acct_a") == (False, False, False)


def test_delete_keeps_records_when_ses_fails(h, table):
    add(h, "acme.com")
    h.sesv2.delete_email_identity.side_effect = client_error("TooManyRequestsException")
    assert call(h, "DELETE /admin/domains/{domain}", domain="acme.com", account_id="acct_a")[0] == 502
    assert claim_exists(table, "acme.com", "acct_a") == (True, True, True)


def test_unknown_route(h):
    assert call(h, "PATCH /admin/domains")[0] == 404
