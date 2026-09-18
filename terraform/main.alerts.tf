# Alerting, and the reason it is worth its cost on a £13/month deployment.
#
# Every workload here is scale-to-zero or a scheduled job that runs for four
# minutes and stops. Nothing is watching at 06:00, and `agent_runs` only records
# a failure the job survived long enough to write — a replica killed before it
# opens its row leaves no evidence at all. These rules read the platform's own
# metric store, so they need no agent, no synthetic traffic and no replica kept
# warm: the two things this design cannot afford.
#
# Metric alert rules are a few pence a month each. The log alert at the bottom
# is the expensive one and is evaluated hourly for that reason.

# ---------------------------------------------------------------------------
# Where alerts go
# ---------------------------------------------------------------------------

# An ARM role receiver rather than an address, because this repository is
# public and `terraform/environments/*.tfvars` are committed — the same reason
# the summary recipient lives in Key Vault. Azure resolves the role to whoever
# currently holds it on the subscription, so there is nothing to commit, nothing
# to put in state, and nothing to update when the person changes.
#
# Key Vault is not an option here: a `data` source on the vault would fail the
# stg and prd plans in CI, which plan against resource groups that do not exist.
resource "azurerm_monitor_action_group" "this" {
  name                = module.naming.monitor_action_group.name
  resource_group_name = azurerm_resource_group.this.name

  # Max 12 characters, and it is what appears in the SMS and email subject.
  short_name = substr(var.project_name, 0, 12)

  arm_role_receiver {
    name    = "subscription-owners"
    role_id = local.owner_role_definition_id

    # The common schema is the one that stays stable across alert types, so a
    # webhook added later does not have to parse three different payloads.
    use_common_alert_schema = true
  }

  # A second receiver alongside the role, not instead of it: the Owner role
  # resolves to the work account, and a run that fails at 06:00 is worth hearing
  # about wherever you actually read mail. Empty disables it; `dynamic` rather
  # than a static block so an empty string means no receiver rather than a
  # receiver with no address.
  dynamic "email_receiver" {
    for_each = var.alert_email_address == "" ? [] : [var.alert_email_address]
    content {
      name                    = "configured-address"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# The jobs and the database
#
# One resource for both rules: they differ only in what they watch, and
# every one of them wants the same name shape, action group and tags. What each
# watches — and why that threshold — lives beside it in `locals.alerts.tf`.
# Job failures used to be a third entry here; see `job_failed` below for why
# they moved to a log alert instead.
# ---------------------------------------------------------------------------

resource "azurerm_monitor_metric_alert" "this" {
  for_each = local.metric_alerts

  name                = "alert-${local.alert_name_prefix}-${each.key}"
  resource_group_name = azurerm_resource_group.this.name
  scopes              = [each.value.scope]
  description         = each.value.description
  severity            = each.value.severity
  frequency           = each.value.frequency
  window_size         = each.value.window

  criteria {
    metric_namespace = each.value.namespace
    metric_name      = each.value.metric
    aggregation      = each.value.aggregation
    operator         = each.value.operator
    threshold        = each.value.threshold

    # Absent on the rules that filter nothing — a `dimension` block with no
    # values is not the same as no block, and would match no time series.
    dynamic "dimension" {
      for_each = each.value.dimension == null ? [] : [each.value.dimension]
      content {
        name     = dimension.value.name
        operator = "Include"
        values   = dimension.value.values
      }
    }
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Job failures
#
# Tried first as a metric alert on `Microsoft.App/jobs`' `Executions` gauge,
# identical in shape to the two rules above. Verified live on 2026-09-18 that
# it never fires: a deliberately-triggered failed execution held
# `Executions{state=Failed}` at 1 for several minutes — comfortably inside a
# 15-minute window against a 5-minute evaluation — and
# `Microsoft.AlertsManagement/alerts` still showed nothing for the resource
# group days later. The rule was correct by every check Terraform can express;
# this reads as a platform-side gap in alerting on that particular
# metric/resource combination.
#
# `ContainerAppSystemLogs_CL` is what actually found the failure during that
# investigation and it ingests reliably, so this queries it instead for the
# three Reason_s values a real crash loop is observed to emit:
# `ContainerCrashing` (the exec/OCI failure itself), `BackoffLimitExceeded`
# (the job giving up), and `StartError` (the replica's own failure record).
#
# One rule for all three jobs, not three rules, the same DRY reasoning as the
# metric alert above — but a log query has no `dimension` block, so
# `resource_id_column` does the equivalent job: it is what makes this fire as
# three independently tracked alerts, one per job ARM ID, rather than a single
# alert that can only ever say "one of the three jobs failed."
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "job_failed" {
  name                = "alert-${local.alert_name_prefix}-job-failed"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  description         = "A container app job execution crashed instead of running to completion."
  severity            = 1

  scopes                = [azurerm_log_analytics_workspace.this.id]
  evaluation_frequency  = "PT5M"
  window_duration       = "PT15M"
  target_resource_types = ["Microsoft.App/jobs"]

  criteria {
    # The case() maps the log's plain job name back to the ARM ID
    # `resource_id_column` needs, without a second `where` per job.
    query = <<-KQL
      ContainerAppSystemLogs_CL
      | where Reason_s in ("ContainerCrashing", "BackoffLimitExceeded", "StartError")
      | extend JobArmId = case(
          JobName_s == "${azurerm_container_app_job.agent.name}", "${azurerm_container_app_job.agent.id}",
          JobName_s == "${azurerm_container_app_job.daily_summary.name}", "${azurerm_container_app_job.daily_summary.id}",
          JobName_s == "${azurerm_container_app_job.weekly_review.name}", "${azurerm_container_app_job.weekly_review.id}",
          ""
        )
      | where JobArmId != ""
    KQL

    time_aggregation_method = "Count"
    resource_id_column      = "JobArmId"
    operator                = "GreaterThan"
    threshold               = 0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# The monitoring watching itself
# ---------------------------------------------------------------------------

# Reaching `daily_quota_gb` stops ingestion for the rest of the day, which
# silently disables every log this workspace holds — including the ones that
# would explain why. Nothing in the metric store reports it, so this is the one
# rule that has to be a log query, and log alerts are the expensive kind.
# Evaluated hourly over a 24-hour window for that reason: measured ingestion is
# ~0.3% of the cap, so the event this catches is a runaway, and an hour's
# notice of a runaway is plenty.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "log_quota" {
  name                = module.naming.monitor_scheduled_query_rules_alert.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  description         = "Log ingestion is approaching the workspace daily cap, past which logging stops."
  severity            = 2

  scopes                = [azurerm_log_analytics_workspace.this.id]
  evaluation_frequency  = "PT1H"
  window_duration       = "P1D"
  target_resource_types = ["Microsoft.OperationalInsights/workspaces"]

  criteria {
    # `Usage` is billed volume in megabytes and is the same figure the cap is
    # applied to. `IsBillable` matters: the free data types do not count
    # towards the quota, so including them would warn on volume that cannot
    # reach the cap.
    query = <<-KQL
      Usage
      | where TimeGenerated > ago(1d) and IsBillable == true
      | summarize IngestedGb = sum(Quantity) / 1024
    KQL

    time_aggregation_method = "Total"
    metric_measure_column   = "IngestedGb"
    operator                = "GreaterThan"
    threshold               = local.log_quota_alert_gb

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  tags = local.tags
}

# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

# `MAX_RUN_COST_USD` caps one agent run's model spend, which is the only thing
# in this system that can run away on its own. Nothing caps Azure. This is the
# backstop for the other direction: a resource added by hand in the portal, a
# workload profile attached to the environment, or a storage tier raised and
# forgotten — each of which bills quietly and none of which the application
# ledger can see.
#
# Notifications go to the same roles as the alerts, for the same public-repo
# reason, and 100% is `Forecasted` rather than `Actual`: hearing that the month
# will overrun while there is still a month left to act is the useful warning.
resource "azurerm_consumption_budget_resource_group" "this" {
  name              = "budget-${local.alert_name_prefix}"
  resource_group_id = azurerm_resource_group.this.id

  amount     = var.monthly_budget_amount
  time_grain = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  notification {
    enabled        = true
    threshold      = 80
    threshold_type = "Actual"
    operator       = "GreaterThan"
    contact_roles  = ["Owner"]
  }

  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Forecasted"
    operator       = "GreaterThan"
    contact_roles  = ["Owner"]
  }

  lifecycle {
    # Azure refuses a start date in a past month on create but reports the
    # stored one thereafter, so leaving this tracked makes the first apply of a
    # new month a spurious replacement.
    ignore_changes = [time_period[0].start_date]
  }
}
