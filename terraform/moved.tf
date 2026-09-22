# Preserve the addresses of dedicated resources for environments that keep them.
moved {
  from = azurerm_log_analytics_workspace.this
  to   = azurerm_log_analytics_workspace.this[0]
}

moved {
  from = azurerm_application_insights.this
  to   = azurerm_application_insights.this[0]
}

moved {
  from = azurerm_container_app_environment.this
  to   = azurerm_container_app_environment.this[0]
}

moved {
  from = azurerm_monitor_scheduled_query_rules_alert_v2.log_quota
  to   = azurerm_monitor_scheduled_query_rules_alert_v2.log_quota[0]
}
