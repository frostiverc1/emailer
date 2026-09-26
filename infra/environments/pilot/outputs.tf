output "api_url" {
  value = module.api.api_url
}

output "user_pool_id" {
  value = module.api.user_pool_id
}

output "user_pool_client_id" {
  value = module.api.user_pool_client_id
}

output "ops_table_name" {
  value = module.storage.ops_table_name
}

output "queue_url" {
  value = module.queue.queue_url
}

output "ses_configuration_set_name" {
  value = module.email.ses_configuration_set_name
}

output "acm_validation_record" {
  description = "Add this CNAME in brandflyers.com's DNS (a different AWS account) to prove ownership."
  value       = module.api.acm_validation_record
}

output "custom_domain_target" {
  description = "Point custom_domain at this with an ALIAS or CNAME record, in the same DNS."
  value       = module.api.custom_domain_target
}

output "custom_domain_url" {
  value = module.api.custom_domain_url
}
