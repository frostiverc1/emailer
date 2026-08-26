resource "aws_sns_topic" "feedback" {
  name = "${var.name_prefix}-feedback"
  tags = var.tags
}

data "archive_file" "feedback" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/feedback"
  output_path = "${path.module}/../../../build/feedback.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "feedback" {
  name               = "${var.name_prefix}-feedback"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "feedback" {
  name = "${var.name_prefix}-feedback"
  role = aws_iam_role.feedback.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = var.suppression_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

resource "aws_lambda_function" "feedback" {
  function_name    = "${var.name_prefix}-feedback"
  role             = aws_iam_role.feedback.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.feedback.output_path
  source_code_hash = data.archive_file.feedback.output_base64sha256
  memory_size      = 128
  timeout          = 10

  environment {
    variables = {
      SUPPRESSION_TABLE_NAME = var.suppression_table_name
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "feedback" {
  name              = "/aws/lambda/${aws_lambda_function.feedback.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

resource "aws_sns_topic_subscription" "feedback" {
  topic_arn = aws_sns_topic.feedback.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.feedback.arn
}

resource "aws_lambda_permission" "sns_invoke" {
  statement_id  = "AllowSNSInvokeFeedback"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.feedback.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.feedback.arn
}
