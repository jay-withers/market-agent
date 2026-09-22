resource "azurerm_log_analytics_workspace" "this" {
  count = var.shared_platform == null ? 1 : 0

  name                = module.naming.log_analytics_workspace.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"

  # 30 days is both the provider minimum and free (31 are included).
  retention_in_days = 30

  # Log volume is the largest cost risk here — everything else is scale-to-zero
  # or per-operation. This keeps ingestion (workspace plus the Application
  # Insights below, which writes into it) inside Azure Monitor's 5 GB/month
  # grant. The figure lives in locals so the alert watching it cannot drift.
  daily_quota_gb = local.log_daily_quota_gb

  tags = local.tags
}

resource "azurerm_application_insights" "this" {
  count = var.shared_platform == null ? 1 : 0

  name                = module.naming.application_insights.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  application_type    = "web"
  workspace_id        = local.log_analytics_workspace_id

  # Defaults to 100 GB/day.
  daily_data_cap_in_gb = 0.1

  tags = local.tags
}


# ---------------------------------------------------------------------------
# Platform logs
#
# Only the two resources that have something to say and say it rarely. There is
# deliberately **no** diagnostic setting on the Container Apps environment: it
# already ships console and system logs to this workspace via
# `log_analytics_workspace_id`, and a diagnostic setting would ingest the same
# lines a second time. Shared environment logging is configured by the platform.
#
# `AllMetrics` is off everywhere for the same reason. Metrics are already in the
# platform metric store, free to query and free to alert on — routing them into
# Log Analytics pays to store a second copy of what main.alerts.tf reads for
# nothing.
# ---------------------------------------------------------------------------

# The server error log: start-up failures, connection refusals, and the
# authentication errors that this project has already found hard to read
# correctly from the client side alone. Quiet in normal operation.
resource "azurerm_monitor_diagnostic_setting" "postgres" {
  name                       = module.naming.monitor_diagnostic_setting.name
  target_resource_id         = azurerm_postgresql_flexible_server.this.id
  log_analytics_workspace_id = local.log_analytics_workspace_id

  enabled_log {
    category = "PostgreSQLLogs"
  }

  # The query-store and session categories are deliberately absent: they are
  # per-statement and per-session emissions, which is the one shape of log that
  # could reach the daily cap on a database this small.
}

# Who read which secret, and when. The vault holds the DeepSeek key, the
# Alpaca credentials and the summary recipient, and it is the only place in the
# system where a credential is handed out — so the read is worth recording.
# Volume is a handful of events per job run.
resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  name                       = module.naming.monitor_diagnostic_setting.name
  target_resource_id         = azurerm_key_vault.this.id
  log_analytics_workspace_id = local.log_analytics_workspace_id

  enabled_log {
    category = "AuditEvent"
  }
}
