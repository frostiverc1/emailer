variable "name_prefix" {
  description = "Prefix for resource names, e.g. email-service-pilot"
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "SQS visibility timeout, should match the worker Lambda timeout"
  type        = number
  default     = 60
}

variable "alarm_email" {
  description = "Email address to notify when the DLQ receives messages. Empty string skips the subscription."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common tags applied to all resources in this module"
  type        = map(string)
  default     = {}
}
