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

variable "queue_url" {
  type = string
}

variable "queue_arn" {
  type = string
}

variable "ses_region" {
  description = "AWS region where customer domains are verified as SES identities"
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

variable "stripe_webhook_secret" {
  description = "The secret used to verify Stripe webhook signatures. Unused while the stripe_webhook Lambda is commented out (lambda.tf)."
  type        = string
  default     = ""
}
