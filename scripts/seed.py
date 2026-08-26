"""Seed script for pilot bootstrapping: one service, one API key, one sample template.

Requires OPS_TABLE_NAME and SEED_SES_FROM_EMAIL env vars. SEED_SES_FROM_EMAIL must
already be a verified SES identity (see docs/email-service-pilot-lld.md section 7).
"""
import boto3
import os
import uuid

table = boto3.resource("dynamodb").Table(os.environ["OPS_TABLE_NAME"])

SES_FROM_EMAIL = os.environ["SEED_SES_FROM_EMAIL"]
ALLOWED_ORIGINS = os.environ.get("SEED_ALLOWED_ORIGINS", "http://localhost:3000").split(",")

SERVICE_ID = "svc_gencoft_internal"
API_KEY = f"gk_{uuid.uuid4().hex[:24]}"

table.put_item(Item={
    "PK": f"SVC#{SERVICE_ID}",
    "SK": "META",
    "name": "Gencoft Internal",
    "provider_type": "ses",
    "ses_from_email": SES_FROM_EMAIL,
    "oauth_status": "connected",
})

table.put_item(Item={
    "PK": f"APIKEY#{API_KEY}",
    "SK": "META",
    "service_id": SERVICE_ID,
    "allowed_origins": set(ALLOWED_ORIGINS),
    "daily_limit": 500,
    "active": True,
})

table.put_item(Item={
    "PK": f"SVC#{SERVICE_ID}",
    "SK": "TPL#tpl_welcome",
    "subject_tpl": "Welcome, {{ to_name }}!",
    "html_tpl": "<h1>Hello {{ to_name }}</h1><p>{{ message }}</p>",
    "text_tpl": "Hello {{ to_name }}\n\n{{ message }}",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
})

print(f"Seeded service_id={SERVICE_ID}")
print(f"API key: {API_KEY}")
