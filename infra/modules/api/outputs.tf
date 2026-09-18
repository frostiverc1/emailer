output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "api_id" {
  value = aws_apigatewayv2_api.this.id
}

output "user_pool_id" {
  value = aws_cognito_user_pool.customers.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
