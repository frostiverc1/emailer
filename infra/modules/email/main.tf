# Pilot approach: individually verified sender addresses, no domain/DKIM verification.
# Each identity triggers a confirmation email that must be clicked before it can send.
resource "aws_ses_email_identity" "this" {
  for_each = toset(var.ses_verified_emails)
  email    = each.value
}

resource "aws_ses_configuration_set" "this" {
  name = var.name_prefix
}

resource "aws_ses_event_destination" "sns" {
  name                   = "${var.name_prefix}-sns-events"
  configuration_set_name = aws_ses_configuration_set.this.name
  enabled                = true
  matching_types         = ["bounce", "complaint"]

  sns_destination {
    topic_arn = aws_sns_topic.feedback.arn
  }
}
