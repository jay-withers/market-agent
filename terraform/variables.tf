# variables.tf — module input variables.

variable "environment" {
  description = "Deployment environment. Drives environment-specific behaviour (naming, sizing, etc.) as the module grows."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prd"], var.environment)
    error_message = "environment must be one of: dev, stg, prd."
  }
}

variable "location" {
  description = "Azure region the resource group is created in."
  type        = string
  default     = "westeurope"
}

variable "tags" {
  description = "Tags applied to all resources created by this module, merged with (and taking precedence over) the module's default tags (`environment`, `managed-by`)."
  type        = map(string)
  default     = {}
}

# Add further module input variables below.
#
# Example:
#
# variable "name" {
#   description = "Name applied to created resources."
#   type        = string
# }
