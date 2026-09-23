"""Shared fixtures. Everything runs offline: moto fakes DynamoDB in-process and SES is a MagicMock."""
import importlib.util
import os
from pathlib import Path
from unittest import mock

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent
TABLE_NAME = "emailer-ops-test"
ATTACHMENTS_BUCKET = "emailer-attachments-test"

# Fake credentials so nothing can ever reach a real AWS account.
os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "OPS_TABLE_NAME": TABLE_NAME,
    "SES_REGION": "us-east-1",
    "ATTACHMENTS_BUCKET_NAME": ATTACHMENTS_BUCKET,
    "USAGE_PLAN_NAME_PREFIX": "emailer-test-",
})


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        yield ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def load_handler(name, table):
    """Import src/<name>/handler.py fresh, pointed at the moto table, with SES mocked."""
    spec = importlib.util.spec_from_file_location(f"{name}_handler", ROOT / "src" / name / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.table = table
    if hasattr(module, "sesv2"):
        module.sesv2 = mock.MagicMock()
    return module
