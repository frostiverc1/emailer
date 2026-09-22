output "ops_table_name" {
  value = aws_dynamodb_table.ops.name
}

output "ops_table_arn" {
  value = aws_dynamodb_table.ops.arn
}

output "attachments_bucket_name" {
  value = aws_s3_bucket.attachments.id
}

output "attachments_bucket_arn" {
  value = aws_s3_bucket.attachments.arn
}
