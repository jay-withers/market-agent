variable "shared_platform" {
  description = "Existing platform resources in the deployment subscription. Null creates dedicated resources. Platform retention, ingestion caps and workspace-wide alerts remain platform-owned."
  type = object({
    subscription_id                = string
    resource_group_name            = string
    log_analytics_workspace_name   = string
    application_insights_name      = string
    container_app_environment_name = string
    workload_profile_name          = optional(string, "Consumption")
  })
  default = null
}

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


variable "image_registry" {
  description = "Registry and repository prefix the workload images are pulled from. Public packages on ghcr.io deliberately: a private one would need a `registry` block and a Key Vault-backed pull secret on every workload, and there is no Azure Container Registry because ACR Basic is a flat monthly charge with no consumption tier."
  type        = string
  default     = "ghcr.io/jay-withers/market-agent"
}

# One tag per image rather than one shared between them, matching the
# lifecycle.ignore_changes split in main.container-apps.tf and
# main.container-apps-jobs.tf — see those for why.
#
# These now only seed the *first* revision, at `terraform apply` time on a
# brand-new environment. Every deploy after that is `make deploy` (az cli),
# which is why they're no longer wired through the Makefile's `deploy` target —
# a plan against an existing environment reports no change here even when the
# running image has moved on, because the container's actual image and env are
# ignored. Don't read a stale-looking default as the deployed version; check
# `az containerapp show` (or the dashboard's own reported build) instead.
#
# Defaults are a published release rather than a sentinel, so a bare
# `terraform apply` against a fresh environment deploys something real. Note
# the `v` prefix: cd-publish pushes each image under the release tag `vX.Y.Z`
# and the commit's short SHA, and `0.4.0` without it is not a tag that exists —
# the pull would fail at revision start-up rather than at plan time.
variable "marketagent_image_tag" {
  # NOTE: this default names a tag published under the old ghcr.io
  # .../investagent path (see the rename PR that renamed the image to
  # marketagent). It is only wrong for a brand-new environment created before
  # a real marketagent image is published under a release tag — dev already
  # exists and ignores this variable after its first revision, so this is not
  # an issue until stg/prd are ever actually applied. Bump it once CI has
  # published a genuine vX.Y.Z under the new name.
  description = "Immutable tag of the marketagent image — the API and all three jobs — used only to seed the first revision on create. `make deploy` (az cli) owns it on every environment after that; see lifecycle.ignore_changes on azurerm_container_app.api and the three azurerm_container_app_job resources. Must not be `latest`: Container Apps creates a revision only when the template changes, so re-pushing a moving tag deploys nothing at all and reports success."
  type        = string
  default     = "v0.12.0"

  validation {
    condition     = !contains(["latest", "main", "unset"], var.marketagent_image_tag)
    error_message = "marketagent_image_tag must be an immutable tag. A moving tag re-pushed under the same name changes no revision template, so Container Apps deploys nothing and reports success. Pass -var marketagent_image_tag=$(git rev-parse --short HEAD)."
  }
}

variable "dashboard_image_tag" {
  description = "Immutable tag of the dashboard image, used only to seed the first revision on create. `make deploy` (az cli) owns it after that; see lifecycle.ignore_changes on azurerm_container_app.dashboard. Must not be `latest`, for the same reason as marketagent_image_tag."
  type        = string
  default     = "v0.12.0"

  validation {
    condition     = !contains(["latest", "main", "unset"], var.dashboard_image_tag)
    error_message = "dashboard_image_tag must be an immutable tag. A moving tag re-pushed under the same name changes no revision template, so Container Apps deploys nothing and reports success. Pass -var dashboard_image_tag=$(git rev-parse --short HEAD)."
  }
}

variable "dashboard_custom_domain_name" {
  description = "Custom hostname to bind to the dashboard container app, e.g. `marketagent.jaywithers.uk`, with a free Azure-managed certificate. Empty (the default) creates neither the managed certificate nor the binding — used to keep the domain, which is globally bound to one environment, off the stg/prd plan legs while only dev is applied. Requires the CNAME (to the dashboard's default `*.azurecontainerapps.io` FQDN) and the `asuid.<name>` TXT (domain verification ID) records to already exist at the DNS provider before apply, since Azure validates both during certificate issuance."
  type        = string
  default     = ""
}

variable "agent_dry_run" {
  description = "Simulate trades without submitting orders to Alpaca. Set false to submit to the paper endpoint. Seeds the agent job on creation; existing jobs ignore environment changes, so also update DRY_RUN with az containerapp job update."
  type        = bool
  default     = false
  nullable    = false
}

variable "agent_cron_expression" {
  description = "Schedule for every pot's agent job, as a 5-field cron expression evaluated in UTC. The pots run concurrently, each on its own DeepSeek key."
  type        = string
  default     = "0 6 * * *"
}

variable "daily_summary_cron_expression" {
  description = "Schedule for the daily summary job, as a 5-field cron expression evaluated in UTC."
  type        = string
  default     = "0 21 * * *"
}

variable "weekly_review_cron_expression" {
  description = "Schedule for the weekly review job, as a 5-field cron expression evaluated in UTC. Must fall after that day's daily summary, whose valuation it reports as the week's close."
  type        = string
  default     = "0 22 * * 0"
}

variable "alert_email_address" {
  description = "Address added to the alert action group as an email receiver, alongside the ARM role receiver that reaches whoever holds `Owner` on the subscription. Committed deliberately: this repository is public, so the default is permanent in its history — an address is an identifier rather than a credential, the same reasoning that puts a named human's object ID and UPN in `main.database.tf`. Set it empty to send to the Owner role alone, or override with `TF_VAR_alert_email_address` to keep a different address out of git. Unlike this, the *summary* recipient stays in Key Vault: it is changed without a redeploy and is read at runtime."
  type        = string
  default     = "withersj888@outlook.com"

  validation {
    # Printable ASCII either side of the `@`, matching the check
    # `Set-KeyVaultSecrets.ps1` applies to the summary recipient. A curly quote
    # pasted from somewhere that autocorrects is invisible in most output and
    # has already cost this project one day's summary.
    condition     = var.alert_email_address == "" || can(regex("^[!-?A-~]+@[!-?A-~]+\\.[!-?A-~]+$", var.alert_email_address))
    error_message = "alert_email_address must be empty or a plain-ASCII email address."
  }
}

variable "monthly_budget_amount" {
  description = "Monthly Azure spend, in the subscription's billing currency, above which the budget notifies. The deployment bills roughly 13/month, effectively all of it the PostgreSQL server; the default leaves room for a month of experimentation without alerting on normal operation. This governs notifications only — Azure budgets never stop anything spending."
  type        = number
  default     = 25

  validation {
    condition     = var.monthly_budget_amount > 0
    error_message = "monthly_budget_amount must be greater than zero."
  }
}

variable "budget_start_date" {
  description = "First day of the budget's first period, which Azure requires to be the first of a month. Ignored after creation — see the `lifecycle` block in `main.alerts.tf` — so it only matters on a fresh apply, and a date in an already-past month is rejected on create."
  type        = string
  default     = "2026-09-01T00:00:00Z"

  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-01T00:00:00Z$", var.budget_start_date))
    error_message = "budget_start_date must be the first of a month, as YYYY-MM-01T00:00:00Z."
  }
}

variable "broker_sync_cron_expression" {
  description = "Refresh the Alpaca account mirror and order status without placing orders or calling the model. Shared by every pot's sync job."
  type        = string
  default     = "*/5 * * * *"
}
