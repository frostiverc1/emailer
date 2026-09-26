module "storage" {
  source      = "../../modules/storage"
  name_prefix = var.name_prefix
  tags        = var.tags
}

module "queue" {
  source      = "../../modules/queue"
  name_prefix = var.name_prefix
  alarm_email = var.alarm_email
  tags        = var.tags
}

module "email" {
  source              = "../../modules/email"
  name_prefix         = var.name_prefix
  ses_verified_emails = var.ses_verified_emails
  tags                = var.tags
}

module "worker" {
  source                     = "../../modules/worker"
  name_prefix                = var.name_prefix
  ops_table_name             = module.storage.ops_table_name
  ops_table_arn              = module.storage.ops_table_arn
  queue_arn                  = module.queue.queue_arn
  ses_region                 = var.aws_region
  max_receive_count          = module.queue.max_receive_count
  alarm_topic_arn            = module.queue.alarm_topic_arn
  attachments_bucket_name    = module.storage.attachments_bucket_name
  attachments_bucket_arn     = module.storage.attachments_bucket_arn
  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret
  tags                       = var.tags
}

module "api" {
  source                     = "../../modules/api"
  name_prefix                = var.name_prefix
  ops_table_name             = module.storage.ops_table_name
  ops_table_arn              = module.storage.ops_table_arn
  queue_url                  = module.queue.queue_url
  queue_arn                  = module.queue.queue_arn
  ses_region                 = var.aws_region
  attachments_bucket_name    = module.storage.attachments_bucket_name
  attachments_bucket_arn     = module.storage.attachments_bucket_arn
  stripe_webhook_secret      = var.stripe_webhook_secret
  google_oauth_client_id     = var.google_oauth_client_id
  google_oauth_client_secret = var.google_oauth_client_secret
  google_oauth_redirect_uri  = var.google_oauth_redirect_uri
  gmail_frontend_return_url  = var.gmail_frontend_return_url
  custom_domain              = var.custom_domain
  tags                       = var.tags
}
