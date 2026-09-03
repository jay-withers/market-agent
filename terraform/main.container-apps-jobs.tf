# Cron expressions are evaluated in UTC, so the wall-clock time shifts by an
# hour with British Summer Time. schedule_trigger_config is ForceNew: changing a
# schedule replaces the job rather than updating it.

module "naming_agent" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "agent"]
}

# "summary", not "daily-summary": caj-marketagent-dev-daily-summary is 33
# characters against the 32 that container app jobs allow, and the naming module
# would silently truncate it to caj-marketagent-dev-daily-summar. The job's own
# container is still named daily-summary below.
module "naming_daily_summary" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "summary"]
}

# Market data and news in, analysis and risk rules applied, decisions recorded.
#
# One measured run takes about 4 minutes against the 1800-second timeout, so the
# bound is comfortable rather than tight. A run terminated on that timeout still
# closes its `agent_runs` row: Container Apps sends SIGTERM first, and the CLI
# turns it into an exception so the failure is recorded.
resource "azurerm_container_app_job" "agent" {
  name                         = module.naming_agent.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 1800
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.agent_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "agent"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      # `schedule`, not the CLI's `manual` default, so `agent_runs.trigger`
      # distinguishes a cron firing from someone running it by hand.
      command = ["investagent"]
      args    = ["agent", "--trigger", "schedule"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags
}

# Performance, the day's trades, benchmark comparison, email.
resource "azurerm_container_app_job" "daily_summary" {
  name                         = module.naming_daily_summary.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 1800
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.daily_summary_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "daily-summary"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["investagent"]
      args    = ["summary"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags
}
