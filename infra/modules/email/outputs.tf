output "ses_configuration_set_name" {
  value = aws_ses_configuration_set.this.name
}

output "sns_topic_arn" {
  value = aws_sns_topic.feedback.arn
}
