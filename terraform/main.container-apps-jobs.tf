# Cron expressions are evaluated in UTC, so the wall-clock time shifts by an
# hour with British Summer Time. schedule_trigger_config is ForceNew: changing a
# schedule replaces the job rather than updating it.
#
# One agent job and one sync job per pot, from local.pots: each pot trades its
# own Alpaca paper account, so the unit of scheduling is one pot. The pots all
# start at the same time and run concurrently — they share nothing but the
# database. daily_summary and weekly_review stay singular: they read every pot
# and produce one combined, comparative email/review. See apps/marketagent's
# jobs/summary.py and jobs/weekly.py.

# `agent-<pot>` rather than `<pot>`, so the job names say what they run.
module "naming_agent" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  for_each = local.pots
  source   = "Azure/naming/azurerm"
  version  = "~> 0.4"
  suffix   = [var.project_name, var.environment, "agent-${each.key}"]
}

module "naming_sync" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  for_each = local.pots
  source   = "Azure/naming/azurerm"
  version  = "~> 0.4"
  suffix   = [var.project_name, var.environment, "sync-${each.key}"]
}

# Market data and news in, analysis and risk rules applied, decisions recorded
# — one pot's fixed watchlist.
#
# A run's length is its analyses, made one at a time at about 38s each (median
# 30s, p90 around 75s, measured on 2026-09-24). A 50-ticker pot with news on
# every ticker is about 35 minutes of analysis, so an hour leaves room without
# hiding a run that has gone wrong. A run terminated on the timeout still
# closes its `agent_runs` row: Container Apps sends SIGTERM first, and the CLI
# turns it into an exception so the failure is recorded.
resource "azurerm_container_app_job" "agent" {
  for_each = local.pots

  name                         = module.naming_agent[each.key].container_app_job.name
  container_app_environment_id = local.container_app_environment_id
  workload_profile_name        = local.container_app_workload_profile_name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 3600
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
      name   = "agent-${each.key}"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      # `schedule`, not the CLI's `manual` default, so `agent_runs.trigger`
      # distinguishes a cron firing from someone running it by hand.
      command = ["marketagent"]
      args    = ["agent", "--trigger", "schedule", "--portfolio", each.key]

      dynamic "env" {
        for_each = local.agent_env[each.key]
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # Same split as azurerm_container_app.api: `make deploy` (az cli) owns the
  # image and IMAGE_TAG after the first revision. See that resource's comment
  # for why the whole env list is ignored rather than just IMAGE_TAG's entry.
  # That includes DRY_RUN and every per-pot override in local.pots: changing
  # one on an existing job needs `az containerapp job update --set-env-vars`.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]

    # The naming module truncates silently, and the app derives the pot's
    # Key Vault secret names from the same string, so a long name would fail
    # at run time rather than here. accounts.py enforces the same rule.
    precondition {
      condition     = can(regex("^[a-z][a-z0-9-]{0,5}$", each.key))
      error_message = "Pot names are at most six lower-case characters: caj-<project>-<env>-agent-<pot> must fit the 32 characters a job name allows."
    }
  }
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

# "weekly", not "weekly-review": caj-marketagent-dev-weekly-review is 33
# characters against the 32 that container app jobs allow — the same trap the
# daily summary hit. The job's own container is still named weekly-review below.
module "naming_weekly_review" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "weekly"]
}

# Performance, the day's trades, benchmark comparison, email — one combined
# email covering every pot, and their comparison, in one send.
resource "azurerm_container_app_job" "daily_summary" {
  name                         = module.naming_daily_summary.container_app_job.name
  container_app_environment_id = local.container_app_environment_id
  workload_profile_name        = local.container_app_workload_profile_name
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

      command = ["marketagent"]
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

  # See azurerm_container_app_job.agent above: `make deploy` owns image and
  # IMAGE_TAG from the first revision onward.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]
  }
}


# The week in review: is the machinery working, and what should change —
# across every pot, with a comparison between them.
#
# Sunday at 22:00 UTC by default, an hour after that day's summary, because it
# reads the summary's valuation as the week's closing figure. Reads only — no
# market data, no broker, one model call — so the 1800-second bound is loose
# rather than considered.
resource "azurerm_container_app_job" "weekly_review" {
  name                         = module.naming_weekly_review.container_app_job.name
  container_app_environment_id = local.container_app_environment_id
  workload_profile_name        = local.container_app_workload_profile_name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 1800
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.weekly_review_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "weekly-review"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["weekly"]

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

  # See azurerm_container_app_job.agent above: `make deploy` owns image and
  # IMAGE_TAG from the first revision onward.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]
  }
}

# Read-only broker requests for one pot: cash, positions and order status,
# every five minutes. No model calls, orders or email.
resource "azurerm_container_app_job" "sync" {
  for_each = local.pots

  name                         = module.naming_sync[each.key].container_app_job.name
  container_app_environment_id = local.container_app_environment_id
  workload_profile_name        = local.container_app_workload_profile_name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 120
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.broker_sync_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "sync-${each.key}"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["sync", "--portfolio", each.key]

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

  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]
  }
}
