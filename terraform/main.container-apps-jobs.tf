# Cron expressions are evaluated in UTC, so the wall-clock time shifts by an
# hour with British Summer Time. schedule_trigger_config is ForceNew: changing a
# schedule replaces the job rather than updating it.
#
# Two accounts, two agent jobs and two sync jobs: static-100 and dynamic-500
# trade independently against separate Alpaca paper accounts, so the unit of
# scheduling has to be one account, not the whole watchlist. daily_summary and
# weekly_review stay singular — they read both accounts and produce one
# combined, comparative email/review, so duplicating those two would just
# produce two reports a reader has to hold in their head at once. See
# apps/marketagent's jobs/summary.py and jobs/weekly.py.

module "naming_agent100" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "agent100"]
}

module "naming_agent500" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "agent500"]
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

# Market data and news in, analysis and risk rules applied, decisions recorded
# — for static-100, the frozen 101-name watchlist.
#
# One measured run of the original 10-ticker watchlist took about 4 minutes
# against a 1800-second timeout. 45 minutes here is ~10x that, matching the
# ~10x larger watchlist and rounded well up as a ceiling to revise from real
# runs, not a measured figure. A run terminated on the timeout still closes
# its `agent_runs` row: Container Apps sends SIGTERM first, and the CLI turns
# it into an exception so the failure is recorded.
resource "azurerm_container_app_job" "agent100" {
  name                         = module.naming_agent100.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 2700
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
      name   = "agent100"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      # `schedule`, not the CLI's `manual` default, so `agent_runs.trigger`
      # distinguishes a cron firing from someone running it by hand.
      command = ["marketagent"]
      args    = ["agent", "--trigger", "schedule", "--portfolio", "static-100"]

      dynamic "env" {
        for_each = local.agent100_env
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
  # This includes DRY_RUN: changing agent_dry_run on an existing job also needs
  # an explicit `az containerapp job update --set-env-vars DRY_RUN=...`.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]
  }
}

# Same as agent100, but for dynamic-500 — the S&P 500 watchlist the rebalance
# job keeps in sync. Staggered ten minutes after agent100's schedule, not
# simultaneous, so the two do not both hit the DeepSeek API at the same
# instant. 3 hours is the roughest estimate in this file: a purely sequential
# loop (see MarketAgent's own docs on why concurrency is explicitly out of
# scope) over up to 500 tickers is the part of this deployment most likely to
# need its timeout revised upward from real runs.
resource "azurerm_container_app_job" "agent500" {
  name                         = module.naming_agent500.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 10800
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.agent500_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "agent500"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["agent", "--trigger", "schedule", "--portfolio", "dynamic-500"]

      dynamic "env" {
        for_each = local.agent500_env
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

# Performance, the day's trades, benchmark comparison, email — one combined
# email covering both accounts, and their comparison, in one send.
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

  # See azurerm_container_app_job.agent100 above: `make deploy` owns image and
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
# across both accounts, with a comparison between them.
#
# Sunday at 22:00 UTC by default, an hour after that day's summary, because it
# reads the summary's valuation as the week's closing figure. Reads only — no
# market data, no broker, one model call — so the 1800-second bound is loose
# rather than considered.
resource "azurerm_container_app_job" "weekly_review" {
  name                         = module.naming_weekly_review.container_app_job.name
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

  # See azurerm_container_app_job.agent100 above: `make deploy` owns image and
  # IMAGE_TAG from the first revision onward.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
      template[0].container[0].command,
    ]
  }
}

module "naming_sync100" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "sync100"]
}

module "naming_sync500" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "sync500"]
}

# Read-only broker requests for static-100; no model calls, orders or email.
resource "azurerm_container_app_job" "sync100" {
  name                         = module.naming_sync100.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
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
      name   = "sync100"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["sync", "--portfolio", "static-100"]

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

# Same as sync100, for dynamic-500. Up to 500 positions rather than 101, so a
# little more headroom than sync100's timeout as cheap insurance — still a
# read-only REST call, nowhere near what would need the 45-minute class of
# bound the agent jobs carry.
resource "azurerm_container_app_job" "sync500" {
  name                         = module.naming_sync500.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 180
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
      name   = "sync500"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["sync", "--portfolio", "dynamic-500"]

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

module "naming_rebalance" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "rebalance"]
}

# Keeps dynamic-500's watchlist in sync with real S&P 500 membership. Monthly,
# not daily — see jobs/rebalance.py — and scheduled well before agent500's
# daily run so a membership change lands before that day's analysis, not after
# it. Calls no Alpaca or DeepSeek API, only Wikipedia, so the timeout is short
# and DRY_RUN/ALPACA_TRADING_BASE_URL are irrelevant here.
resource "azurerm_container_app_job" "rebalance" {
  name                         = module.naming_rebalance.container_app_job.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location

  replica_timeout_in_seconds = 300
  replica_retry_limit        = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  schedule_trigger_config {
    cron_expression          = var.rebalance_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "rebalance"
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      command = ["marketagent"]
      args    = ["rebalance"]

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
