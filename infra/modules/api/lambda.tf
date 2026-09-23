data "aws_region" "current" {}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- validate Lambda (public POST /v1/send) ---

data "archive_file" "validate" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/validate"
  output_path = "${path.module}/../../../build/validate.zip"
}

resource "aws_iam_role" "validate" {
  name               = "${var.name_prefix}-validate"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "validate" {
  name = "${var.name_prefix}-validate"
  role = aws_iam_role.validate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = var.ops_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.queue_arn
      },
      {
        # PutObject is for signing presigned POSTs (validate never uploads bytes itself). GetObject
        # is for HeadObject, checking an attachment's owner and size before an email is queued.
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${var.attachments_bucket_arn}/users/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

resource "aws_lambda_function" "validate" {
  function_name    = "${var.name_prefix}-validate"
  role             = aws_iam_role.validate.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.validate.output_path
  source_code_hash = data.archive_file.validate.output_base64sha256
  memory_size      = 256
  timeout          = 10

  environment {
    variables = {
      OPS_TABLE_NAME          = var.ops_table_name
      QUEUE_URL               = var.queue_url
      ATTACHMENTS_BUCKET_NAME = var.attachments_bucket_name
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "validate" {
  name              = "/aws/lambda/${aws_lambda_function.validate.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# --- admin Lambda (behind the Cognito JWT authorizer, see auth.tf) ---

data "archive_file" "admin" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/admin"
  output_path = "${path.module}/../../../build/admin.zip"
}

resource "aws_iam_role" "admin" {
  name               = "${var.name_prefix}-admin"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "admin" {
  name = "${var.name_prefix}-admin"
  role = aws_iam_role.admin.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = var.ops_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        # Manages the account's API Gateway key and its usage-plan membership (created and moved
        # between tiers here, since AWS is now the source of truth for the key itself).
        Effect = "Allow"
        Action = [
          "apigateway:POST",
          "apigateway:DELETE",
          "apigateway:GET",
        ]
        Resource = [
          "arn:aws:apigateway:${data.aws_region.current.name}::/apikeys",
          "arn:aws:apigateway:${data.aws_region.current.name}::/apikeys/*",
          "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans",
          "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*",
          "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*/keys",
          "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*/keys/*",
          "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*/usage",
        ]
      },
    ]
  })
}

resource "aws_lambda_function" "admin" {
  function_name    = "${var.name_prefix}-admin"
  role             = aws_iam_role.admin.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.admin.output_path
  source_code_hash = data.archive_file.admin.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      OPS_TABLE_NAME = var.ops_table_name
      # Usage plans are named "<this>-<tier>" (main.tf); looked up by name at runtime rather than
      # passed as an env var, since that would create a dependency cycle (the plans' api_stages
      # block depends on the deployment, which depends on this Lambda's own integration).
      USAGE_PLAN_NAME_PREFIX = "${var.name_prefix}-"
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "admin" {
  name              = "/aws/lambda/${aws_lambda_function.admin.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# --- stripe_webhook Lambda (public POST /v1/stripe-webhook, no Cognito auth) ---
# Split out from admin so this public, self-authenticating route doesn't share a deployment or an
# IAM role with the Cognito-gated account-management routes.
# Not deployed yet — Stripe isn't configured (no keys/signing secret). Uncomment to enable.

# data "archive_file" "stripe_webhook" {
#   type        = "zip"
#   source_dir  = "${path.module}/../../../src/stripe_webhook"
#   output_path = "${path.module}/../../../build/stripe_webhook.zip"
# }

# resource "aws_iam_role" "stripe_webhook" {
#   name               = "${var.name_prefix}-stripe-webhook"
#   assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
#   tags               = var.tags
# }

# resource "aws_iam_role_policy" "stripe_webhook" {
#   name = "${var.name_prefix}-stripe-webhook"
#   role = aws_iam_role.stripe_webhook.id
#   policy = jsonencode({
#     Version = "2012-10-17"
#     Statement = [
#       {
#         # Only the account's plan and its current key — nothing about templates or emails.
#         Effect   = "Allow"
#         Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
#         Resource = var.ops_table_arn
#       },
#       {
#         Effect   = "Allow"
#         Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
#         Resource = "arn:aws:logs:*:*:*"
#       },
#       {
#         # Moves the account's key to the new plan's usage plan when a subscription changes.
#         Effect = "Allow"
#         Action = [
#           "apigateway:GET",
#           "apigateway:POST",
#           "apigateway:DELETE",
#         ]
#         Resource = [
#           "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans",
#           "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*",
#           "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*/keys",
#           "arn:aws:apigateway:${data.aws_region.current.name}::/usageplans/*/keys/*",
#         ]
#       },
#     ]
#   })
# }

# resource "aws_lambda_function" "stripe_webhook" {
#   function_name    = "${var.name_prefix}-stripe-webhook"
#   role             = aws_iam_role.stripe_webhook.arn
#   runtime          = "python3.12"
#   handler          = "handler.handler"
#   filename         = data.archive_file.stripe_webhook.output_path
#   source_code_hash = data.archive_file.stripe_webhook.output_base64sha256
#   memory_size      = 128
#   timeout          = 10

#   environment {
#     variables = {
#       OPS_TABLE_NAME         = var.ops_table_name
#       STRIPE_WEBHOOK_SECRET  = var.stripe_webhook_secret
#       USAGE_PLAN_NAME_PREFIX = "${var.name_prefix}-"
#     }
#   }

#   tags = var.tags
# }

# resource "aws_cloudwatch_log_group" "stripe_webhook" {
#   name              = "/aws/lambda/${aws_lambda_function.stripe_webhook.function_name}"
#   retention_in_days = 14
#   tags              = var.tags
# }
