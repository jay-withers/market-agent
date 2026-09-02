variable "project_name" {
  description = "Project name included in every resource name. Lowercase only (several resource types reject uppercase), and no longer than 15 characters: beyond that `ca-<project>-<env>-dashboard` exceeds the 32 characters container apps allow and gets silently truncated. Changing this is the way to resolve a clash on the globally unique Key Vault or PostgreSQL server names, including a name still held by a soft-deleted vault."
  type        = string
  default     = "marketagent"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,14}$", var.project_name))
    error_message = "project_name must be 2-15 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "location" {
  # Constrained from two directions: the `allowed-locations-dev` policy denies
  # anything outside westeurope/northeurope, and this subscription is blocked
  # from provisioning PostgreSQL in westeurope. northeurope is the only region
  # satisfying both. Probe the capabilities API before changing it — see the
  # README.
  description = "Azure region resources are created in."
  type        = string
  default     = "northeurope"
}

variable "tags" {
  description = "Tags applied to all resources, merged with (and taking precedence over) the default tags (`environment`, `managed-by`)."
  type        = map(string)
  default     = {}
}

variable "key_vault_administrator_object_ids" {
  description = "Additional Entra object IDs granted `Key Vault Secrets Officer`, so they can populate secret values. Whoever runs `terraform apply` gets this automatically."
  type        = list(string)
  default     = []
}

variable "postgres_sku_name" {
  description = "Compute SKU. Burstable B1ms is the smallest Flexible Server offers; there is no serverless or auto-pause tier, so this bills per hour for as long as the server exists."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_version" {
  description = "Major PostgreSQL version."
  type        = string
  default     = "17"
}

variable "postgres_storage_mb" {
  description = "Provisioned storage. Billed on this figure rather than bytes used, and it can never be reduced — only grown — so it sits at Azure's 32 GB minimum."
  type        = number
  default     = 32768
}

variable "postgres_storage_tier" {
  description = "Storage performance tier. P4 is the default for 32 GB; a higher tier raises the monthly bill for IOPS this workload does not need."
  type        = string
  default     = "P4"
}

variable "postgres_zone" {
  description = "Availability zone the server is placed in. northeurope offers 1, 2 and 3 for B1ms."
  type        = string
  default     = "1"
}


variable "agent_cron_expression" {
  description = "Schedule for the AI trading agent job, as a 5-field cron expression evaluated in UTC."
  type        = string
  default     = "0 6 * * *"
}

variable "daily_summary_cron_expression" {
  description = "Schedule for the daily summary job, as a 5-field cron expression evaluated in UTC."
  type        = string
  default     = "0 21 * * *"
}
