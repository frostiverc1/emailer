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
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = var.ops_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = var.suppression_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.queue_arn
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
      OPS_TABLE_NAME         = var.ops_table_name
      SUPPRESSION_TABLE_NAME = var.suppression_table_name
      QUEUE_URL              = var.queue_url
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "validate" {
  name              = "/aws/lambda/${aws_lambda_function.validate.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

# --- admin Lambda (internal only, no auth for pilot, protected by AWS_IAM route auth) ---

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
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "admin" {
  name              = "/aws/lambda/${aws_lambda_function.admin.function_name}"
  retention_in_days = 14
  tags              = var.tags
}
