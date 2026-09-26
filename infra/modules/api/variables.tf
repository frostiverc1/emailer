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

variable "google_oauth_client_id" {
  description = "OAuth 2.0 client ID from the Google Cloud project used for the Gmail-send fallback (gmail.tf)."
  type        = string
}

variable "google_oauth_client_secret" {
  description = "OAuth 2.0 client secret matching google_oauth_client_id."
  type        = string
  sensitive   = true
}

variable "google_oauth_redirect_uri" {
  description = "Must exactly match a redirect URI registered on the Google OAuth client, e.g. https://<api-id>.execute-api.<region>.amazonaws.com/dev/admin/gmail/callback."
  type        = string
}

variable "gmail_frontend_return_url" {
  description = "Dashboard page the browser lands on after the Gmail connect flow finishes (success or failure)."
  type        = string
}

variable "custom_domain" {
  description = "Optional custom domain fronting the API (e.g. emailer.brandflyers.com), see custom_domain.tf. Blank skips it entirely and the API is only reachable at its execute-api.amazonaws.com URL."
  type        = string
  default     = ""
}
