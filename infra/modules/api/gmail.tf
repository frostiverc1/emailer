# --- gmail Lambda: OAuth connect/callback/status/disconnect for the Gmail-fallback sender option
# (alongside verified-domain sending). /admin/gmail/callback is public (Google's redirect carries
# no Cognito token); every other route sits behind Cognito, same as domains.tf.

data "archive_file" "gmail" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/gmail"
  output_path = "${path.module}/../../../build/gmail.zip"
}

resource "aws_iam_role" "gmail" {
  name               = "${var.name_prefix}-gmail"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "gmail" {
  name = "${var.name_prefix}-gmail"
  role = aws_iam_role.gmail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GMAILSTATE# rows (the OAuth CSRF/account binding) and each account's ACCT#/GMAIL row.
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
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

resource "aws_lambda_function" "gmail" {
  function_name    = "${var.name_prefix}-gmail"
  role             = aws_iam_role.gmail.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.gmail.output_path
  source_code_hash = data.archive_file.gmail.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      OPS_TABLE_NAME             = var.ops_table_name
      GOOGLE_OAUTH_CLIENT_ID     = var.google_oauth_client_id
      GOOGLE_OAUTH_CLIENT_SECRET = var.google_oauth_client_secret
      GOOGLE_OAUTH_REDIRECT_URI  = var.google_oauth_redirect_uri
      GMAIL_FRONTEND_RETURN_URL  = var.gmail_frontend_return_url
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "gmail" {
  name              = "/aws/lambda/${aws_lambda_function.gmail.function_name}"
  retention_in_days = 14
  tags              = var.tags
}
