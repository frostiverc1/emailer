variable "aws_region" {
  description = "AWS region for the pilot deploy"
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = "emailer"
}

variable "ses_verified_emails" {
  description = "Sender addresses to verify as SES identities. Each requires clicking a confirmation link sent to that inbox."
  type        = list(string)
}

variable "alarm_email" {
  description = "Email address to notify when the DLQ receives messages. Empty string skips the subscription."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}

variable "stripe_webhook_secret" {
  description = "The secret used to verify Stripe webhook signatures. Unused while the stripe_webhook Lambda is commented out (infra/modules/api/lambda.tf)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_oauth_client_id" {
  description = "OAuth 2.0 client ID from the Google Cloud project, for the Gmail-send fallback."
  type        = string
}

variable "google_oauth_client_secret" {
  description = "OAuth 2.0 client secret matching google_oauth_client_id."
  type        = string
  sensitive   = true
}

variable "google_oauth_redirect_uri" {
  description = "Must exactly match a redirect URI registered on the Google OAuth client: <api invoke url>/admin/gmail/callback."
  type        = string
}

variable "gmail_frontend_return_url" {
  description = "Dashboard page the browser lands on after the Gmail connect flow finishes."
  type        = string
}

variable "custom_domain" {
  description = "Optional custom domain fronting the API, e.g. emailer.brandflyers.com. Blank skips it (infra/modules/api/custom_domain.tf)."
  type        = string
  default     = ""
}
