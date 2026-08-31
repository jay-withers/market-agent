resource "azurerm_container_app_environment" "this" {
  name                       = module.naming.container_app_environment.name_unique
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  # Inferred from the workspace in azurerm 4.x, but reverts to an "" default in
  # 5.x, which would then show as a perpetual diff.
  logs_destination = "log-analytics"

  # No workload_profile block on purpose: that keeps this Consumption-only, which
  # is what lets the apps scale to zero and the jobs bill only while running.
  tags = local.tags
}
