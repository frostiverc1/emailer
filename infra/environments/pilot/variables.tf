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
  description = "The secret used to verify Stripe webhook signatures"
  type        = string
  sensitive   = true
}
