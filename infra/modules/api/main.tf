resource "aws_apigatewayv2_api" "this" {
  name          = "${var.name_prefix}-api"
  protocol_type = "HTTP"
  tags          = var.tags

  # Lets browser-based callers (dev-console.html, a future dashboard) call the API cross-origin.
  # HTTP APIs handle preflight OPTIONS automatically once this is set, no separate OPTIONS routes needed.
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["authorization", "content-type", "x-api-key"]
  }
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true
  tags        = var.tags
}

# --- public send route, no route-level auth: the validate Lambda itself checks the x-api-key header ---

resource "aws_apigatewayv2_integration" "validate" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.validate.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "send" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /v1/send"
  target    = "integrations/${aws_apigatewayv2_integration.validate.id}"
}

resource "aws_apigatewayv2_route" "attachments_upload_url" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /v1/attachments/upload-url"
  target    = "integrations/${aws_apigatewayv2_integration.validate.id}"
}

resource "aws_lambda_permission" "validate_invoke" {
  statement_id  = "AllowAPIGatewayInvokeValidate"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.validate.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# --- admin routes: require a Cognito login (auth.tf). The Lambda scopes everything to the caller's account ---

resource "aws_apigatewayv2_integration" "admin" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.admin.invoke_arn
  payload_format_version = "2.0"
}

locals {
  admin_routes = [
    "GET /admin/keys",
    "POST /admin/keys",
    "GET /admin/templates",
    "POST /admin/templates",
    "GET /admin/templates/{id}",
    "PUT /admin/templates/{id}",
    "DELETE /admin/templates/{id}",
    "GET /admin/usage/{api_key}",
    "GET /admin/emails",
    "GET /admin/emails/{request_id}",
    "GET /admin/plan",
    "POST /admin/plan",
  ]
}

resource "aws_apigatewayv2_route" "admin" {
  for_each  = toset(local.admin_routes)
  api_id    = aws_apigatewayv2_api.this.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.admin.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

# Webhook route is public (verifies its own signature)
resource "aws_apigatewayv2_route" "stripe_webhook" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "POST /v1/stripe-webhook"
  target    = "integrations/${aws_apigatewayv2_integration.admin.id}"
}

resource "aws_lambda_permission" "admin_invoke" {
  statement_id  = "AllowAPIGatewayInvokeAdmin"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.admin.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}

# --- domain routes: same Cognito login as the admin routes above. The Lambda is in domains.tf ---

resource "aws_apigatewayv2_integration" "domains" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.domains.invoke_arn
  payload_format_version = "2.0"
}

locals {
  domain_routes = [
    "POST /admin/domains",
    "GET /admin/domains",
    "GET /admin/domains/{domain}",
    "DELETE /admin/domains/{domain}",
    "POST /admin/domains/{domain}/check",
    "POST /admin/domains/{domain}/retry",
  ]
}

resource "aws_apigatewayv2_route" "domains" {
  for_each  = toset(local.domain_routes)
  api_id    = aws_apigatewayv2_api.this.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.domains.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "domains_invoke" {
  statement_id  = "AllowAPIGatewayInvokeDomains"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.domains.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
