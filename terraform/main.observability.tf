resource "azurerm_log_analytics_workspace" "this" {
  name                = module.naming.log_analytics_workspace.name_unique
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"

  # 30 days is both the provider minimum and free (31 are included).
  retention_in_days = 30

  # Log volume is the largest cost risk here — everything else is scale-to-zero
  # or per-operation. This keeps ingestion (workspace plus the Application
  # Insights below, which writes into it) inside Azure Monitor's 5 GB/month
  # grant.
  daily_quota_gb = 0.15

  tags = local.tags
}

resource "azurerm_application_insights" "this" {
  name                = module.naming.application_insights.name_unique
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.this.id

  # Defaults to 100 GB/day.
  daily_data_cap_in_gb = 0.1

  tags = local.tags
}
