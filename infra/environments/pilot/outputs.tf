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
