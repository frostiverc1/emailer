variable "name_prefix" {
  description = "Prefix for resource names, e.g. email-service-pilot"
  type        = string
}

variable "ses_verified_emails" {
  description = "Individual sender addresses to verify as SES identities (pilot approach, no domain verification)"
  type        = list(string)
}

variable "tags" {
  description = "Common tags applied to all resources in this module"
  type        = map(string)
  default     = {}
}
