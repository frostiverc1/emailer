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

# --- custom_domain.tf: paste these into the other AWS account's Route 53 hosted zone by hand. ---

output "acm_validation_record" {
  description = "Add this CNAME in the domain's DNS to prove ownership. Only present once (during apply, before it's validated)."
  value = var.custom_domain != "" ? {
    name  = tolist(aws_acm_certificate.custom_domain[0].domain_validation_options)[0].resource_record_name
    type  = tolist(aws_acm_certificate.custom_domain[0].domain_validation_options)[0].resource_record_type
    value = tolist(aws_acm_certificate.custom_domain[0].domain_validation_options)[0].resource_record_value
  } : null
}

output "custom_domain_target" {
  description = "Point custom_domain at this with an ALIAS (Route 53) or CNAME record."
  value       = var.custom_domain != "" ? aws_api_gateway_domain_name.custom_domain[0].regional_domain_name : null
}

output "custom_domain_url" {
  description = "The API's URL once custom_domain's DNS is set up. Use <this>/admin/gmail/callback as the Google OAuth redirect URI."
  value       = var.custom_domain != "" ? "https://${var.custom_domain}" : null
}
