# --- customer login: Cognito user pool, email is the username ---
# The /admin/* routes (main.tf) require a Cognito access token. The Lambdas derive the caller's
# account id from the token's sub, so one Cognito user is one account for now.

data "aws_region" "current" {}

resource "aws_cognito_user_pool" "customers" {
  name                     = "${var.name_prefix}-customers"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Cognito's built-in sender, capped at ~50 emails a day. Switch to SES before real signups.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  tags = var.tags
}

# Public client for the browser: no secret, plain username/password auth so the dev console can log
# in with a fetch call and no SDK.
resource "aws_cognito_user_pool_client" "web" {
  name                          = "${var.name_prefix}-web"
  user_pool_id                  = aws_cognito_user_pool.customers.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.this.id
  name             = "${var.name_prefix}-cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    # Access tokens carry client_id instead of aud; API Gateway checks either against this list.
    audience = [aws_cognito_user_pool_client.web.id]
    issuer   = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.customers.id}"
  }
}
