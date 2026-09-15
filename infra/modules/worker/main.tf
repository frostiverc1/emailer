data "archive_file" "worker" {
  type        = "zip"
  source_dir  = "${path.module}/../../../src/worker"
  output_path = "${path.module}/../../../build/worker.zip"
  excludes    = ["requirements.txt"]
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

resource "aws_iam_role" "worker" {
  name               = "${var.name_prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "worker" {
  name = "${var.name_prefix}-worker"
  role = aws_iam_role.worker.id
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
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
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

resource "aws_lambda_function" "worker" {
  function_name    = "${var.name_prefix}-worker"
  role             = aws_iam_role.worker.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.worker.output_path
  source_code_hash = data.archive_file.worker.output_base64sha256
  memory_size      = 256
  timeout          = 60
  layers           = [aws_lambda_layer_version.jinja2.arn]

  environment {
    variables = {
      OPS_TABLE_NAME    = var.ops_table_name
      SES_REGION        = var.ses_region
      MAX_RECEIVE_COUNT = var.max_receive_count
      # Must exceed the Lambda timeout and stay below the queue visibility timeout.
      LEASE_SECONDS = 90
    }
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${aws_lambda_function.worker.function_name}"
  retention_in_days = 14
  tags              = var.tags
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = var.queue_arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1
}

# Permanent failures skip the DLQ, so an account-wide SES block needs its own alarm.
resource "aws_cloudwatch_log_metric_filter" "ses_blocked" {
  name           = "${var.name_prefix}-ses-blocked"
  log_group_name = aws_cloudwatch_log_group.worker.name
  pattern        = "?\"error_code=SES_ACCOUNT_PAUSED\" ?\"error_code=SES_DAILY_QUOTA\""

  metric_transformation {
    name      = "SesBlockedSends"
    namespace = "${var.name_prefix}/worker"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "ses_blocked" {
  alarm_name          = "${var.name_prefix}-ses-blocked"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.ses_blocked.metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.ses_blocked.metric_transformation[0].namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "Worker sends are failing because SES paused the account or the daily quota is exhausted"
  alarm_actions       = [var.alarm_topic_arn]

  tags = var.tags
}
