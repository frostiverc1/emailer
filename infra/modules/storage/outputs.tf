output "ops_table_name" {
  value = aws_dynamodb_table.ops.name
}

output "ops_table_arn" {
  value = aws_dynamodb_table.ops.arn
}

output "suppression_table_name" {
  value = aws_dynamodb_table.suppression.name
}

output "suppression_table_arn" {
  value = aws_dynamodb_table.suppression.arn
}
