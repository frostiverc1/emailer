"""Step 2: "Check now", retrying failed verification, and the scheduled checker."""
import time
from unittest import mock

from test_domains import TOKENS, ZONE, add, call, claim_exists, client_error, h, seed_domain  # noqa: F401 (h is a fixture)

DAY = 86400


def identity(dkim_status, mail_from_status="PENDING", tokens=TOKENS, zone=ZONE):
    return {
        "IdentityType": "DOMAIN",
        "DkimAttributes": {"Status": dkim_status, "Tokens": tokens, "SigningHostedZone": zone},
        "MailFromAttributes": {"MailFromDomain": "bounce.x", "MailFromDomainStatus": mail_from_status},
    }


def get(table, domain):
    return table.get_item(Key={"PK": f"DOMAIN#{domain}", "SK": "META"}).get("Item")


def seed(table, domain, status, account_id="acct_a", checked_ago=DAY, expires_in=DAY, **fields):
    now = int(time.time())
    fields.setdefault("dkim_tokens", TOKENS)
    fields.setdefault("signing_hosted_zone", ZONE)
    fields.setdefault("mail_from_domain", f"bounce.{domain}")
    seed_domain(table, domain, account_id, status, last_checked_at=now - checked_ago, claim_expires_at=now + expires_in, **fields)


def check_now(h, domain, account_id="acct_a"):
    return call(h, "POST /admin/domains/{domain}/check", domain=domain, account_id=account_id)


def retry(h, domain, account_id="acct_a"):
    return call(h, "POST /admin/domains/{domain}/retry", domain=domain, account_id=account_id)


def run_checker(h, remaining_ms=900_000):
    context = mock.Mock()
    context.get_remaining_time_in_millis.return_value = remaining_ms
    return h.handler({"action": "check_domains"}, context)


# --- Check now ---

def test_check_now_marks_domain_verified(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.return_value = identity("SUCCESS", mail_from_status="SUCCESS")

    status, body = check_now(h, "acme.com")

    assert status == 200
    assert (body["status"], body["dkim_status"], body["mail_from_status"]) == ("verified", "SUCCESS", "SUCCESS")
    assert body["verified_at"] is not None
    assert body["claim_expires_at"] is None
    item = get(table, "acme.com")
    assert "claim_expires_at" not in item
    assert int(item["last_checked_at"]) >= int(time.time()) - 5


def test_check_now_is_rate_limited_per_domain(h, table):
    seed(table, "acme.com", "pending", checked_ago=2)
    status, body = check_now(h, "acme.com")
    assert (status, body["status"]) == (200, "pending")
    h.sesv2.get_email_identity.assert_not_called()


def test_check_now_on_other_accounts_domain(h, table):
    seed(table, "acme.com", "pending")
    assert check_now(h, "acme.com", account_id="acct_b")[0] == 404
    h.sesv2.get_email_identity.assert_not_called()


def test_check_now_while_still_being_added(h, table):
    seed(table, "acme.com", "creating")
    assert check_now(h, "acme.com")[0] == 409
    h.sesv2.get_email_identity.assert_not_called()


def test_check_now_ses_error(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.side_effect = client_error("TooManyRequestsException")
    status, body = check_now(h, "acme.com")
    assert (status, body["error"]) == (502, "SES error: TooManyRequestsException")
    assert get(table, "acme.com")["status"] == "pending"


def test_identity_missing_from_ses_is_failed(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.side_effect = client_error("NotFoundException")
    body = check_now(h, "acme.com")[1]
    assert (body["status"], body["dkim_status"]) == ("failed", "MISSING")


def test_dkim_failed_is_failed(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.return_value = identity("FAILED", mail_from_status="FAILED")
    assert check_now(h, "acme.com")[1]["status"] == "failed"


def test_temporary_failure_keeps_a_verified_domain_verified(h, table):
    seed(table, "acme.com", "verified", verified_at="2026-09-01T00:00:00.000000Z")
    h.sesv2.get_email_identity.return_value = identity("TEMPORARY_FAILURE", mail_from_status="SUCCESS")
    body = check_now(h, "acme.com")[1]
    assert (body["status"], body["dkim_status"]) == ("verified", "TEMPORARY_FAILURE")


def test_temporary_failure_on_never_verified_domain_is_pending(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.return_value = identity("TEMPORARY_FAILURE")
    assert check_now(h, "acme.com")[1]["status"] == "pending"


def test_previously_verified_domain_whose_records_were_removed_fails(h, table):
    seed(table, "acme.com", "verified", verified_at="2026-09-01T00:00:00.000000Z")
    h.sesv2.get_email_identity.return_value = identity("FAILED", mail_from_status="SUCCESS")
    body = check_now(h, "acme.com")[1]
    assert body["status"] == "failed"
    assert body["verified_at"] == "2026-09-01T00:00:00.000000Z"


def test_refresh_picks_up_new_tokens(h, table):
    seed(table, "acme.com", "pending")
    h.sesv2.get_email_identity.return_value = identity("PENDING", tokens=["new1", "new2", "new3"], zone="dkim.example.amazonses.com")
    records = check_now(h, "acme.com")[1]["records"]
    assert [r["value"] for r in records[:3]] == [f"new{i}.dkim.example.amazonses.com" for i in (1, 2, 3)]


# --- Retry ---

def test_retry_failed_dkim_recreates_identity(h, table):
    seed(table, "acme.com", "failed", dkim_status="FAILED", mail_from_status="FAILED", expires_in=3 * DAY)
    expires_before = int(get(table, "acme.com")["claim_expires_at"])
    h.sesv2.get_email_identity.return_value = identity("PENDING", tokens=["n1", "n2", "n3"])

    status, body = retry(h, "acme.com")

    assert status == 200
    assert [c[0] for c in h.sesv2.method_calls if c[0] != "get_email_identity"] == [
        "delete_email_identity", "create_email_identity", "put_email_identity_mail_from_attributes",
    ]
    assert body["status"] == "pending"
    assert body["records"][0]["name"] == "n1._domainkey.acme.com"
    # Retrying must not extend the claim, or a squatter could hold a domain forever.
    assert int(get(table, "acme.com")["claim_expires_at"]) == expires_before


def test_retry_only_failed_mail_from(h, table):
    seed(table, "acme.com", "verified", verified_at="2026-09-01T00:00:00.000000Z", mail_from_status="FAILED")
    h.sesv2.get_email_identity.return_value = identity("SUCCESS", mail_from_status="PENDING")

    status, body = retry(h, "acme.com")

    assert status == 200
    h.sesv2.delete_email_identity.assert_not_called()
    h.sesv2.create_email_identity.assert_not_called()
    h.sesv2.put_email_identity_mail_from_attributes.assert_called_once_with(
        EmailIdentity="acme.com", MailFromDomain="bounce.acme.com", BehaviorOnMxFailure="USE_DEFAULT_VALUE"
    )
    assert (body["status"], body["mail_from_status"]) == ("verified", "PENDING")


def test_retry_when_nothing_failed(h, table):
    seed(table, "acme.com", "pending", mail_from_status="PENDING")
    status, body = retry(h, "acme.com")
    assert (status, body["error"]) == (409, "Nothing to retry, verification hasn't failed")
    assert h.sesv2.method_calls == []


def test_retry_that_fails_halfway_shows_real_state(h, table):
    seed(table, "acme.com", "failed", dkim_status="FAILED")
    h.sesv2.create_email_identity.side_effect = client_error("TooManyRequestsException")
    h.sesv2.get_email_identity.side_effect = client_error("NotFoundException")

    status, _ = retry(h, "acme.com")

    assert status == 502
    item = get(table, "acme.com")
    assert (item["status"], item["dkim_status"]) == ("failed", "MISSING")


def test_retry_other_accounts_domain(h, table):
    seed(table, "acme.com", "failed")
    assert retry(h, "acme.com", account_id="acct_b")[0] == 404
    assert h.sesv2.method_calls == []


# --- Scheduled checker ---

def test_checker_refreshes_what_is_due(h, table):
    seed(table, "pending.com", "pending", checked_ago=60)
    seed(table, "fresh-verified.com", "verified", verified_at="x", mail_from_status="SUCCESS", checked_ago=3600)
    seed(table, "stale-verified.com", "verified", verified_at="x", mail_from_status="SUCCESS", checked_ago=DAY + 60)
    seed(table, "mailfrom-pending.com", "verified", verified_at="x", mail_from_status="PENDING", checked_ago=60)
    h.sesv2.get_email_identity.side_effect = lambda EmailIdentity: identity("SUCCESS", mail_from_status="SUCCESS")

    result = run_checker(h)

    checked = sorted(c.kwargs["EmailIdentity"] for c in h.sesv2.get_email_identity.call_args_list)
    assert checked == ["mailfrom-pending.com", "pending.com", "stale-verified.com"]
    assert result == {"domains": 4, "checked": 3, "released": 0, "stopped_early": False}
    assert get(table, "pending.com")["status"] == "verified"


def test_checker_releases_expired_unverified_claim(h, table):
    seed(table, "squat.com", "pending", expires_in=-60)
    h.sesv2.get_email_identity.return_value = identity("PENDING")

    result = run_checker(h)

    assert result["released"] == 1
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="squat.com")
    assert claim_exists(table, "squat.com", "acct_a") == (False, False, False)


def test_checker_keeps_expiring_claim_that_just_verified(h, table):
    seed(table, "acme.com", "pending", expires_in=-60)
    h.sesv2.get_email_identity.return_value = identity("SUCCESS")

    result = run_checker(h)

    assert result["released"] == 0
    h.sesv2.delete_email_identity.assert_not_called()
    assert get(table, "acme.com")["status"] == "verified"


def test_checker_never_releases_a_domain_that_was_once_verified(h, table):
    seed(table, "acme.com", "failed", verified_at="2026-09-01T00:00:00.000000Z", expires_in=-60)
    h.sesv2.get_email_identity.return_value = identity("FAILED")
    assert run_checker(h)["released"] == 0
    assert claim_exists(table, "acme.com", "acct_a") == (True, True, True)


def test_checker_handles_crashed_adds(h, table):
    seed(table, "crashed.com", "creating", expires_in=-60)
    seed(table, "in-progress.com", "creating", expires_in=DAY)

    result = run_checker(h)

    assert result["released"] == 1
    h.sesv2.get_email_identity.assert_not_called()
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="crashed.com")
    assert claim_exists(table, "in-progress.com", "acct_a") == (True, True, True)


def test_checker_releases_unverified_subdomain_under_other_accounts_verified_domain(h, table):
    seed(table, "acme.com", "verified", account_id="acct_a", verified_at="x", mail_from_status="SUCCESS", checked_ago=60)
    seed(table, "mail.acme.com", "pending", account_id="acct_b", checked_ago=60)
    seed(table, "news.acme.com", "pending", account_id="acct_a", checked_ago=60)
    h.sesv2.get_email_identity.return_value = identity("PENDING")

    result = run_checker(h)

    assert result["released"] == 1
    h.sesv2.delete_email_identity.assert_called_once_with(EmailIdentity="mail.acme.com")
    assert claim_exists(table, "mail.acme.com", "acct_b") == (False, False, False)
    assert claim_exists(table, "news.acme.com", "acct_a") == (True, True, True)


def test_checker_stops_when_ses_throttles(h, table):
    for name in ("a.com", "b.com", "c.com"):
        seed(table, name, "pending", expires_in=-60)
    h.sesv2.get_email_identity.side_effect = client_error("TooManyRequestsException")

    result = run_checker(h)

    assert result["stopped_early"] is True
    assert h.sesv2.get_email_identity.call_count == 1
    # Nothing is released while its real state is unknown.
    h.sesv2.delete_email_identity.assert_not_called()
    assert result["released"] == 0


def test_checker_stops_when_lambda_time_runs_low(h, table):
    seed(table, "acme.com", "pending")
    result = run_checker(h, remaining_ms=10_000)
    assert result["stopped_early"] is True
    assert h.sesv2.method_calls == []


def test_checker_keeps_claim_when_ses_delete_fails(h, table):
    seed(table, "squat.com", "pending", expires_in=-60)
    h.sesv2.get_email_identity.return_value = identity("PENDING")
    h.sesv2.delete_email_identity.side_effect = client_error("TooManyRequestsException")

    assert run_checker(h)["released"] == 0
    assert claim_exists(table, "squat.com", "acct_a") == (True, True, True)


def test_checker_drops_index_entries_with_no_domain(h, table):
    table.put_item(Item={"PK": "DOMAINS", "SK": "DOMAIN#ghost.com", "domain": "ghost.com"})
    assert run_checker(h)["domains"] == 0
    assert "Item" not in table.get_item(Key={"PK": "DOMAINS", "SK": "DOMAIN#ghost.com"})


def test_refresh_never_recreates_a_domain_deleted_mid_check(h, table):
    seed(table, "acme.com", "pending")

    def deleted_while_asking_ses(EmailIdentity):
        table.delete_item(Key={"PK": "DOMAIN#acme.com", "SK": "META"})
        return identity("SUCCESS")

    h.sesv2.get_email_identity.side_effect = deleted_while_asking_ses
    result = run_checker(h)
    assert result["checked"] == 0
    assert get(table, "acme.com") is None


def test_added_domain_is_found_by_checker(h, table):
    add(h, "acme.com")
    h.sesv2.get_email_identity.return_value = identity("SUCCESS")
    # Just added counts as checked now, so make it due.
    table.update_item(Key={"PK": "DOMAIN#acme.com", "SK": "META"}, UpdateExpression="SET last_checked_at = :z", ExpressionAttributeValues={":z": 0})
    assert run_checker(h)["checked"] == 1
    assert get(table, "acme.com")["status"] == "verified"
