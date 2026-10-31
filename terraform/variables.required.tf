variable "environment" {
  description = "Deployment environment. Drives resource naming and per-environment sizing."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prd"], var.environment)
    error_message = "environment must be one of: dev, stg, prd."
  }
}

variable "key_vault_administrator_object_ids" {
  description = "Entra object IDs granted `Key Vault Secrets Officer`, so they can populate secret values. Listed explicitly rather than taken from whoever runs `terraform apply`, because cd-terraform applies as the pipeline identity. Not committed: CI reads it from the TF_VAR_KEY_VAULT_ADMINISTRATOR_OBJECT_IDS variable github-repos sets on the `dev` environment; a local plan needs TF_VAR_key_vault_administrator_object_ids exported."
  type        = list(string)
}

variable "alert_email_address" {
  description = "Address added to the alert action group as an email receiver, alongside the ARM role receiver that reaches whoever holds `Owner` on the subscription. Set it empty to send to the Owner role alone. Unlike this, the *summary* recipient stays in Key Vault: it is changed without a redeploy and is read at runtime. Not committed: CI reads it from the TF_VAR_ALERT_EMAIL_ADDRESS variable github-repos sets on the `dev` environment; a local plan needs TF_VAR_alert_email_address exported."
  type        = string

  validation {
    # Printable ASCII either side of the `@`, matching the check
    # `Set-KeyVaultSecrets.ps1` applies to the summary recipient. A curly quote
    # pasted from somewhere that autocorrects is invisible in most output and
    # has already cost this project one day's summary.
    condition     = var.alert_email_address == "" || can(regex("^[!-?A-~]+@[!-?A-~]+\\.[!-?A-~]+$", var.alert_email_address))
    error_message = "alert_email_address must be empty or a plain-ASCII email address."
  }
}

variable "postgres_entra_administrator" {
  description = "The Entra user made the PostgreSQL server's Entra administrator: object ID, and the account's real UPN, which ARM makes the PostgreSQL role name and which must match exactly or the login fails. Explicit rather than derived from the deploying principal, so a CI apply doesn't make the OIDC service principal the administrator and lock every human out. Not committed: CI reads it from the TF_VAR_POSTGRES_ENTRA_ADMINISTRATOR variable github-repos sets on the `dev` environment; a local plan needs TF_VAR_postgres_entra_administrator exported."
  type = object({
    object_id      = string
    principal_name = string
  })
}
