locals {
  max_receive_count = 3
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-dlq"
  message_retention_seconds = 14 * 24 * 60 * 60

  tags = var.tags
}

resource "aws_sqs_queue" "send_queue" {
  name                       = "${var.name_prefix}-send-queue"
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = 4 * 24 * 60 * 60
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = local.max_receive_count
  })

  tags = var.tags
}
