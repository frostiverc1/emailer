import json
import os
import logging
from datetime import datetime
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["SUPPRESSION_TABLE_NAME"])

HARD_BOUNCE_THRESHOLD = 3


def handler(event, context):
    for record in event["Records"]:
        message = json.loads(record["Sns"]["Message"])
        notif_type = message.get("notificationType") or message.get("eventType")

        if notif_type == "Bounce":
            _handle_bounce(message)
        elif notif_type == "Complaint":
            _handle_complaint(message)


def _handle_bounce(message):
    bounce = message["bounce"]
    is_permanent = bounce["bounceType"] == "Permanent"

    for recipient in bounce["bouncedRecipients"]:
        email = recipient["emailAddress"]
        result = table.update_item(
            Key={"PK": f"BOUNCE#{email}", "SK": "META"},
            UpdateExpression=(
                "ADD #c :inc "
                "SET #last = :now, "
                "#suppressed = if_not_exists(#suppressed, :false), "
                "#reason = if_not_exists(#reason, :bounce)"
            ),
            ExpressionAttributeNames={
                "#c": "count",
                "#last": "last_bounced_at",
                "#suppressed": "suppressed",
                "#reason": "reason",
            },
            ExpressionAttributeValues={
                ":inc": 1,
                ":now": datetime.utcnow().isoformat(),
                ":false": False,
                ":bounce": "hard_bounce",
            },
            ReturnValues="UPDATED_NEW",
        )

        count = int(result["Attributes"]["count"])
        if is_permanent and count >= HARD_BOUNCE_THRESHOLD:
            table.update_item(
                Key={"PK": f"BOUNCE#{email}", "SK": "META"},
                UpdateExpression="SET #suppressed = :true, #reason = :reason",
                ExpressionAttributeNames={
                    "#suppressed": "suppressed",
                    "#reason": "reason",
                },
                ExpressionAttributeValues={
                    ":true": True,
                    ":reason": f"hard_bounce_count_{count}",
                },
            )
            logger.warning(f"Suppressed {email} after {count} hard bounces")


def _handle_complaint(message):
    for recipient in message["complaint"]["complainedRecipients"]:
        email = recipient["emailAddress"]
        table.update_item(
            Key={"PK": f"BOUNCE#{email}", "SK": "META"},
            UpdateExpression="SET #suppressed = :true, #reason = :reason, #at = :now",
            ExpressionAttributeNames={
                "#suppressed": "suppressed",
                "#reason": "reason",
                "#at": "last_bounced_at",
            },
            ExpressionAttributeValues={
                ":true": True,
                ":reason": "complaint",
                ":now": datetime.utcnow().isoformat(),
            },
        )
        logger.warning(f"Suppressed {email} due to complaint")
