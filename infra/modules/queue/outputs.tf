output "queue_url" {
  value = aws_sqs_queue.send_queue.url
}

output "queue_arn" {
  value = aws_sqs_queue.send_queue.arn
}

output "dlq_arn" {
  value = aws_sqs_queue.dlq.arn
}

output "max_receive_count" {
  value = local.max_receive_count
}

output "alarm_topic_arn" {
  value = aws_sns_topic.dlq_alarm.arn
}
