resource "aws_api_gateway_rest_api" "this" {
  name = "${var.name_prefix}-api"
  tags = var.tags
}

# Every route this API serves, as "METHOD /path". REST API needs one aws_api_gateway_resource per
# path *segment* (unlike the old HTTP API's flat route keys), so the locals below derive the
# resource tree and the method/integration wiring from this single list.
locals {
  routes = {
    "POST /v1/send"                   = { lambda = "validate", auth = "NONE", api_key = true }
    "POST /v1/attachments/upload-url" = { lambda = "validate", auth = "NONE", api_key = true }
    # Stripe isn't set up yet (no keys/signing secret configured). Commented out, not deleted —
    # src/stripe_webhook and its Terraform in lambda.tf are still there, just not wired up.
    # "POST /v1/stripe-webhook"            = { lambda = "stripe_webhook", auth = "NONE", api_key = false }
    "GET /admin/keys"                    = { lambda = "admin", auth = "COGNITO", api_key = false }
    "POST /admin/keys"                   = { lambda = "admin", auth = "COGNITO", api_key = false }
    "GET /admin/templates"               = { lambda = "admin", auth = "COGNITO", api_key = false }
    "POST /admin/templates"              = { lambda = "admin", auth = "COGNITO", api_key = false }
    "GET /admin/templates/{id}"          = { lambda = "admin", auth = "COGNITO", api_key = false }
    "PUT /admin/templates/{id}"          = { lambda = "admin", auth = "COGNITO", api_key = false }
    "DELETE /admin/templates/{id}"       = { lambda = "admin", auth = "COGNITO", api_key = false }
    "GET /admin/emails"                  = { lambda = "admin", auth = "COGNITO", api_key = false }
    "GET /admin/emails/{request_id}"     = { lambda = "admin", auth = "COGNITO", api_key = false }
    "GET /admin/plan"                    = { lambda = "admin", auth = "COGNITO", api_key = false }
    "POST /admin/plan"                   = { lambda = "admin", auth = "COGNITO", api_key = false }
    "POST /admin/domains"                = { lambda = "domains", auth = "COGNITO", api_key = false }
    "GET /admin/domains"                 = { lambda = "domains", auth = "COGNITO", api_key = false }
    "GET /admin/domains/{domain}"        = { lambda = "domains", auth = "COGNITO", api_key = false }
    "DELETE /admin/domains/{domain}"     = { lambda = "domains", auth = "COGNITO", api_key = false }
    "POST /admin/domains/{domain}/check" = { lambda = "domains", auth = "COGNITO", api_key = false }
    "POST /admin/domains/{domain}/retry" = { lambda = "domains", auth = "COGNITO", api_key = false }
  }

  lambda_arns = {
    validate = aws_lambda_function.validate.invoke_arn
    admin    = aws_lambda_function.admin.invoke_arn
    domains  = aws_lambda_function.domains.invoke_arn
    # stripe_webhook = aws_lambda_function.stripe_webhook.invoke_arn
  }

  lambda_function_names = {
    validate = aws_lambda_function.validate.function_name
    admin    = aws_lambda_function.admin.function_name
    domains  = aws_lambda_function.domains.function_name
    # stripe_webhook = aws_lambda_function.stripe_webhook.function_name
  }

  # Just the paths (method stripped), e.g. "/admin/templates/{id}".
  paths = distinct([for r in keys(local.routes) : split(" ", r)[1]])

  # Every path segment combination that needs its own resource node. For "/admin/templates/{id}"
  # that's "/admin", "/admin/templates" and "/admin/templates/{id}".
  path_segments = distinct(flatten([
    for p in local.paths : [
      for i in range(1, length(split("/", p))) : join("/", slice(split("/", p), 0, i + 1))
    ]
  ]))

  # Segment depth (number of parts after the leading slash), grouped so each depth's resources
  # only ever reference the *previous* depth's resource address — never their own. A resource
  # whose parent_id indexes another instance of that same for_each resource makes Terraform treat
  # the whole thing as one graph node and report a false cycle, even though no single instance
  # actually depends on itself; splitting by depth (a different resource address per level) avoids
  # that entirely. 4 levels covers this API's deepest route today (/admin/domains/{domain}/check).
  segments_by_depth = {
    for d in range(1, 5) : d => [
      for s in local.path_segments : s if length(split("/", s)) - 1 == d
    ]
  }

  parent_path = { for s in local.path_segments : s => join("/", slice(split("/", s), 0, length(split("/", s)) - 1)) }
  last_part   = { for s in local.path_segments : s => element(split("/", s), length(split("/", s)) - 1) }
}

resource "aws_api_gateway_resource" "level1" {
  for_each    = toset(local.segments_by_depth[1])
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = local.last_part[each.value]
}

resource "aws_api_gateway_resource" "level2" {
  for_each    = toset(local.segments_by_depth[2])
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level1[local.parent_path[each.value]].id
  path_part   = local.last_part[each.value]
}

resource "aws_api_gateway_resource" "level3" {
  for_each    = toset(local.segments_by_depth[3])
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level2[local.parent_path[each.value]].id
  path_part   = local.last_part[each.value]
}

resource "aws_api_gateway_resource" "level4" {
  for_each    = toset(local.segments_by_depth[4])
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.level3[local.parent_path[each.value]].id
  path_part   = local.last_part[each.value]
}

locals {
  # Every path segment's resource id, regardless of which depth level created it — the single
  # lookup every method/integration/CORS resource below uses.
  resource_ids = merge(
    { for k, v in aws_api_gateway_resource.level1 : k => v.id },
    { for k, v in aws_api_gateway_resource.level2 : k => v.id },
    { for k, v in aws_api_gateway_resource.level3 : k => v.id },
    { for k, v in aws_api_gateway_resource.level4 : k => v.id },
  )
}

resource "aws_api_gateway_method" "this" {
  for_each         = local.routes
  rest_api_id      = aws_api_gateway_rest_api.this.id
  resource_id      = local.resource_ids[split(" ", each.key)[1]]
  http_method      = split(" ", each.key)[0]
  authorization    = each.value.auth == "COGNITO" ? "COGNITO_USER_POOLS" : "NONE"
  authorizer_id    = each.value.auth == "COGNITO" ? aws_api_gateway_authorizer.cognito.id : null
  api_key_required = each.value.api_key
}

resource "aws_api_gateway_integration" "this" {
  for_each                = local.routes
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = local.resource_ids[split(" ", each.key)[1]]
  http_method             = aws_api_gateway_method.this[each.key].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = local.lambda_arns[each.value.lambda]
}

resource "aws_lambda_permission" "invoke" {
  for_each      = local.lambda_function_names
  statement_id  = "AllowAPIGatewayInvoke${title(each.key)}"
  action        = "lambda:InvokeFunction"
  function_name = each.value
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/*"
}

# --- CORS: REST API doesn't handle preflight automatically like HTTP API did, so every resource
# gets its own mock OPTIONS response. Each Lambda's own _resp() adds the Allow-Origin header to its
# real responses (a REST API integration response can't inject headers into a Lambda proxy's body). ---

resource "aws_api_gateway_method" "options" {
  for_each      = toset(local.paths)
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = local.resource_ids[each.value]
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options" {
  for_each          = toset(local.paths)
  rest_api_id       = aws_api_gateway_rest_api.this.id
  resource_id       = local.resource_ids[each.value]
  http_method       = aws_api_gateway_method.options[each.value].http_method
  type              = "MOCK"
  request_templates = { "application/json" = "{\"statusCode\": 200}" }
}

resource "aws_api_gateway_method_response" "options" {
  for_each    = toset(local.paths)
  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = local.resource_ids[each.value]
  http_method = aws_api_gateway_method.options[each.value].http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options" {
  for_each    = toset(local.paths)
  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = local.resource_ids[each.value]
  http_method = aws_api_gateway_method.options[each.value].http_method
  status_code = aws_api_gateway_method_response.options[each.value].status_code
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'authorization,content-type,x-api-key'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
  depends_on = [aws_api_gateway_integration.options]
}

# API Gateway generates these responses itself (auth rejected, missing/invalid api key,
# throttled, unhandled Lambda error) without ever invoking a Lambda, so they never get the
# Access-Control-Allow-Origin header the handlers' own _resp() adds. Without this the browser
# reports a CORS error, masking the real 401/403/429/5xx underneath.
resource "aws_api_gateway_gateway_response" "cors" {
  for_each      = toset(["DEFAULT_4XX", "DEFAULT_5XX"])
  rest_api_id   = aws_api_gateway_rest_api.this.id
  response_type = each.value
  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'authorization,content-type,x-api-key'"
  }
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  triggers = {
    redeployment = sha1(jsonencode({
      routes           = local.routes
      segments         = local.path_segments
      authorizer       = aws_api_gateway_authorizer.cognito.id
      gateway_response = { for k, v in aws_api_gateway_gateway_response.cors : k => v.response_parameters }
    }))
  }
  lifecycle {
    create_before_destroy = true
  }
  depends_on = [
    aws_api_gateway_integration.this,
    aws_api_gateway_integration.options,
    aws_api_gateway_integration_response.options,
    aws_api_gateway_gateway_response.cors,
  ]
}

resource "aws_api_gateway_stage" "dev" {
  deployment_id = aws_api_gateway_deployment.this.id
  rest_api_id   = aws_api_gateway_rest_api.this.id
  stage_name    = "dev"
  tags          = var.tags
}

# --- usage plans: one per pricing tier, gateway-enforced quota and throttle. Kept in sync with the
# PLANS table in src/validate and src/admin handlers (requests_per_month = quota). Rate/burst have
# no prior equivalent in this app; picked as reasonable per-tier defaults, adjust as needed. ---

locals {
  plan_gateway_limits = {
    free         = { quota = 200, rate = 2, burst = 5 }
    personal     = { quota = 2000, rate = 5, burst = 10 }
    professional = { quota = 5000, rate = 10, burst = 20 }
    business     = { quota = 25000, rate = 25, burst = 50 }
  }
}

resource "aws_api_gateway_usage_plan" "this" {
  for_each = local.plan_gateway_limits
  name     = "${var.name_prefix}-${each.key}"

  api_stages {
    api_id = aws_api_gateway_rest_api.this.id
    stage  = aws_api_gateway_stage.dev.stage_name
  }

  throttle_settings {
    rate_limit  = each.value.rate
    burst_limit = each.value.burst
  }

  quota_settings {
    limit  = each.value.quota
    period = "MONTH"
  }

  tags = var.tags
}
