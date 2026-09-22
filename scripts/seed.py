"""Seed script for pilot bootstrapping: one account, one API key, one sample template.

Requires the OPS_TABLE_NAME env var. Sending needs a verified domain owned by the account, and this
script does not create one. Real accounts come from Cognito sign-up (acct_<sub>) and add domains
through the domains API, so this seed account can't send until a DOMAIN# record is added by hand.
"""
import boto3
import os
import uuid

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])

ALLOWED_ORIGINS = os.environ.get("SEED_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

ACCOUNT_ID = "acct_gencoft_internal"
API_KEY = f"gk_{uuid.uuid4().hex[:24]}"

table.put_item(Item={
    "PK": f"ACCT#{ACCOUNT_ID}",
    "SK": "META",
    "name": "Gencoft Internal",
    "plan": "free",
    "created_at": "2024-01-01T00:00:00",
})

table.put_item(Item={
    "PK": f"APIKEY#{API_KEY}",
    "SK": "META",
    "account_id": ACCOUNT_ID,
    "allowed_origins": set(ALLOWED_ORIGINS),
    "active": True,
})

table.put_item(Item={
    "PK": f"ACCT#{ACCOUNT_ID}",
    "SK": "TPL#tpl_welcome",
    "subject_tpl": "Welcome, {{ to_name }}!",
    "html_tpl": "<h1>Hello {{ to_name }}</h1><p>{{ message }}</p>",
    "text_tpl": "Hello {{ to_name }}\n\n{{ message }}",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
})

print(f"Seeded account_id={ACCOUNT_ID}")
print(f"API key: {API_KEY}")
