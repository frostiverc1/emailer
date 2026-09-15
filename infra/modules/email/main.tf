# Pilot approach: individually verified sender addresses, no domain/DKIM verification.
# Each identity triggers a confirmation email that must be clicked before it can send.
resource "aws_ses_email_identity" "this" {
  for_each = toset(var.ses_verified_emails)
  email    = each.value
}

resource "aws_ses_configuration_set" "this" {
  name = var.name_prefix
}

# Account-wide, not per-service: a complaint against one sender suppresses the address for every sender.
resource "aws_sesv2_account_suppression_attributes" "this" {
  suppressed_reasons = ["BOUNCE", "COMPLAINT"]
}
