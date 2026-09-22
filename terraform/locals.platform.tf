locals {
  log_analytics_workspace_id             = var.shared_platform == null ? azurerm_log_analytics_workspace.this[0].id : data.azurerm_log_analytics_workspace.platform[0].id
  log_analytics_workspace_customer_id    = var.shared_platform == null ? azurerm_log_analytics_workspace.this[0].workspace_id : data.azurerm_log_analytics_workspace.platform[0].workspace_id
  application_insights_connection_string = var.shared_platform == null ? azurerm_application_insights.this[0].connection_string : data.azurerm_application_insights.platform[0].connection_string
  container_app_environment_id           = var.shared_platform == null ? azurerm_container_app_environment.this[0].id : data.azurerm_container_app_environment.platform[0].id
  container_app_workload_profile_name    = var.shared_platform == null ? null : var.shared_platform.workload_profile_name

  # Direct Log Analytics ingestion leaves _ResourceId empty. EnvironmentName_s
  # contains the generated DNS prefix, not the Azure resource name.
  container_app_log_environment_name = split(".", var.shared_platform == null ? azurerm_container_app_environment.this[0].default_domain : data.azurerm_container_app_environment.platform[0].default_domain)[0]
}
