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

variable "tags" {
  description = "Common tags applied to all resources in this module"
  type        = map(string)
  default     = {}
}
