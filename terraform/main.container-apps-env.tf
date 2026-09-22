resource "azurerm_container_app_environment" "this" {
  count = var.shared_platform == null ? 1 : 0

  name                       = module.naming.container_app_environment.name
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = local.log_analytics_workspace_id

  # Inferred from the workspace in azurerm 4.x, but reverts to an "" default in
  # 5.x, which would then show as a perpetual diff.
  logs_destination = "log-analytics"

  # No workload_profile block on purpose: that keeps this Consumption-only, which
  # is what lets the apps scale to zero and the jobs bill only while running.
  tags = local.tags
}
