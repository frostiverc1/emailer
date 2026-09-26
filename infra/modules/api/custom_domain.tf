# --- Optional custom domain fronting the API (e.g. emailer.brandflyers.com), instead of the raw
# execute-api.amazonaws.com URL. Needed for Google OAuth: Google refuses execute-api.amazonaws.com
# as a redirect URI since it's a shared public-suffix domain, not one this account owns.
#
# The domain's DNS lives in a different AWS account, so Terraform here can't create the validation
# or final DNS records itself — it requests the cert and creates the mapping, and prints the two
# records (acm_validation_record, custom_domain_target) to add by hand in that account's Route 53.
#
# Everything below is skipped (count = 0) when custom_domain is left blank.

resource "aws_acm_certificate" "custom_domain" {
  count             = var.custom_domain != "" ? 1 : 0
  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = var.tags
}

# Waits for ACM to see the validation CNAME. Terraform polls for it during apply, so adding the
# record (from the acm_validation_record output) while this apply is running is enough — no need
# to run apply twice, as long as it's added before the default 45-minute wait times out.
resource "aws_acm_certificate_validation" "custom_domain" {
  count           = var.custom_domain != "" ? 1 : 0
  certificate_arn = aws_acm_certificate.custom_domain[0].arn
}

resource "aws_api_gateway_domain_name" "custom_domain" {
  count       = var.custom_domain != "" ? 1 : 0
  domain_name = var.custom_domain
  # regional_certificate_arn, not certificate_arn: the latter is only for EDGE-type domains and
  # conflicts with endpoint_configuration.types = ["REGIONAL"] below (AWS rejects both being set).
  regional_certificate_arn = aws_acm_certificate_validation.custom_domain[0].certificate_arn
  security_policy          = "TLS_1_2"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = var.tags
}

resource "aws_api_gateway_base_path_mapping" "custom_domain" {
  count       = var.custom_domain != "" ? 1 : 0
  api_id      = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.dev.stage_name
  domain_name = aws_api_gateway_domain_name.custom_domain[0].domain_name
  # No base_path: the custom domain serves the same paths as the execute-api URL, e.g.
  # https://emailer.brandflyers.com/admin/gmail/callback instead of .../dev/admin/gmail/callback.
}
