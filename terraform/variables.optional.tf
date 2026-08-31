variable "project_name" {
  description = "Project name used as a naming prefix. Lowercase only, and no longer than 12 characters: beyond that the Key Vault name (24 characters, the tightest limit here) gets truncated and loses its uniqueness suffix."
  type        = string
  default     = "investagent"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,11}$", var.project_name))
    error_message = "project_name must be 2-12 lowercase alphanumeric characters, starting with a letter."
  }
}

variable "location" {
  description = "Azure region resources are created in."
  type        = string
  default     = "westeurope"
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

variable "sql_admin_object_id" {
  description = "Entra object ID of the SQL server's administrator. Defaults to whoever runs `terraform apply`. Set it to name a different principal — an Entra group keeps administrator access working regardless of who applies."
  type        = string
  default     = null
}

variable "sql_admin_login_username" {
  description = "Display name shown for the SQL server's Entra administrator. Set it alongside `sql_admin_object_id`, conventionally to that principal's user principal name or group name."
  type        = string
  default     = null
}

variable "sql_database_max_size_gb" {
  description = "Provisioned maximum size of the database. General Purpose storage bills this figure rather than bytes used, so it is deliberately small."
  type        = number
  default     = 2
}

variable "sql_auto_pause_delay_in_minutes" {
  description = "Idle time before the serverless database auto-pauses and stops accruing compute charges. 15 is Azure's minimum; -1 disables auto-pause, meaning compute is billed around the clock."
  type        = number
  default     = 15

  validation {
    condition     = var.sql_auto_pause_delay_in_minutes == -1 || (var.sql_auto_pause_delay_in_minutes >= 15 && var.sql_auto_pause_delay_in_minutes <= 10080)
    error_message = "sql_auto_pause_delay_in_minutes must be -1 (disabled) or between 15 and 10080."
  }
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
