variable "name_prefix" {
  description = "Prefix for resource names, e.g. email-service-pilot"
  type        = string
}

variable "ops_table_name" {
  type = string
}

variable "ops_table_arn" {
  type = string
}

variable "queue_arn" {
  type = string
}

variable "ses_region" {
  description = "AWS region the worker's SES client sends through"
  type        = string
}

variable "max_receive_count" {
  description = "Send queue maxReceiveCount, so the worker can tell when a retry is the last one"
  type        = number
}

variable "alarm_topic_arn" {
  description = "SNS topic notified when sends are blocked by SES account pause or daily quota"
  type        = string
}

variable "attachments_bucket_name" {
  type = string
}

variable "attachments_bucket_arn" {
  type = string
}

variable "tags" {
  description = "Common tags applied to all resources in this module"
  type        = map(string)
  default     = {}
}

variable "google_oauth_client_id" {
  description = "OAuth 2.0 client ID, used to refresh Gmail access tokens when sending via the Gmail fallback."
  type        = string
}

variable "google_oauth_client_secret" {
  description = "OAuth 2.0 client secret matching google_oauth_client_id."
  type        = string
  sensitive   = true
}
