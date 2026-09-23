output "api_url" {
  value = aws_api_gateway_stage.dev.invoke_url
}

output "api_id" {
  value = aws_api_gateway_rest_api.this.id
}

output "user_pool_id" {
  value = aws_cognito_user_pool.customers.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.web.id
}

output "usage_plan_ids" {
  description = "Plan name -> API Gateway usage plan id, so admin's key creation attaches a new key to the right one"
  value       = { for name, plan in aws_api_gateway_usage_plan.this : name => plan.id }
}
