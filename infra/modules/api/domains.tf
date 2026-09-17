# --- domains Lambda: customer domain verification, behind the /admin/domains routes (main.tf) ---
# Internal only like the admin routes: no auth yet, account_id comes from the request.

data "aws_caller_identity" "current" {}

# boto3 layer. The runtime's bundled boto3 may predate DkimAttributes.SigningHostedZone, which the
# domains Lambda needs to show correct DNS records; layers take precedence over runtime-included
# libraries. Built locally with pip, like the worker's Jinja2 layer, so `pip` must be available on
# the machine running `terraform apply`.
resource "null_resource" "boto3_layer_build" {
  triggers = {
    requirements_hash = filesha256("${path.module}/../../../src/domains/requirements.txt")
  }

  provisioner "local-exec" {
    command = "pip install -r ${path.module}/../../../src/domains/requirements.txt -t ${path.module}/../../../build/boto3-layer/python --upgrade --no-compile"
  }
}

data "archive_file" "boto3_layer" {
  type        = "zip"
  source_dir  = "${path.module}/../../../build/boto3-layer"
  output_path = "${path.module}/../../../build/boto3-layer.zip"

  depends_on = [null_resource.boto3_layer_build]
}

resource "aws_lambda_layer_version" "boto3" {
  layer_name          = "${var.name_prefix}-boto3"
  filename            = data.archive_file.boto3_layer.output_path
  source_code_hash    = data.archive_file.boto3_layer.output_base64sha256
  compatible_runtimes = ["python3.12"]
}

data "archive_file" "domains" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/domains"
  output_path = "${path.module}/../../../build/domains.zip"
  excludes    = ["requirements.txt"]
}

resource "aws_iam_role" "domains" {
  name               = "${var.name_prefix}-domains"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "domains" {
  name = "${var.name_prefix}-domains"
  role = aws_iam_role.domains.id
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
        ]
        Resource = var.ops_table_arn
      },
      {
        # Identity setup only. This Lambda can't send email.
        Effect = "Allow"
        Action = [
          "ses:CreateEmailIdentity",
          "ses:GetEmailIdentity",
          "ses:DeleteEmailIdentity",
          "ses:PutEmailIdentityMailFromAttributes",
        ]
        Resource = "arn:aws:ses:${var.ses_region}:${data.aws_caller_identity.current.account_id}:identity/*"
      },
      {
        # Customer domains never contain "@". This keeps a bug from touching the verified sender
        # addresses the pilot sends from.
        Effect   = "Deny"
        Action   = ["ses:*"]
        Resource = "arn:aws:ses:${var.ses_region}:${data.aws_caller_identity.current.account_id}:identity/*@*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

resource "aws_lambda_function" "domains" {
  function_name    = "${var.name_prefix}-domains"
  role             = aws_iam_role.domains.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.domains.output_path
  source_code_hash = data.archive_file.domains.output_base64sha256
  memory_size      = 256
  # API requests are cut off by API Gateway at 30s regardless. The long timeout is for the scheduled
  # checker, which stops itself with 30s to spare.
  timeout = 300
  layers  = [aws_lambda_layer_version.boto3.arn]

  environment {
    variables = {
      OPS_TABLE_NAME = var.ops_table_name
      SES_REGION     = var.ses_region
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "domains" {
  name              = "/aws/lambda/${aws_lambda_function.domains.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# --- scheduled domain status checker ---

resource "aws_iam_role" "domains_scheduler" {
  name = "${var.name_prefix}-domains-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "domains_scheduler" {
  name = "${var.name_prefix}-domains-scheduler"
  role = aws_iam_role.domains_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.domains.arn
    }]
  })
}

resource "aws_scheduler_schedule" "domain_check" {
  name                = "${var.name_prefix}-domain-check"
  schedule_expression = "rate(15 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.domains.arn
    role_arn = aws_iam_role.domains_scheduler.arn
    input    = jsonencode({ action = "check_domains" })
  }
}
