# Supplies tenant_id for the Key Vault and SQL server, and object_id for the
# default Key Vault Secrets Officer and SQL administrator assignments.
data "azurerm_client_config" "current" {}

data "azurerm_log_analytics_workspace" "platform" {
  count = var.shared_platform == null ? 0 : 1

  name                = var.shared_platform.log_analytics_workspace_name
  resource_group_name = var.shared_platform.resource_group_name

  lifecycle {
    precondition {
      condition     = data.azurerm_client_config.current.subscription_id == var.shared_platform.subscription_id
      error_message = "The Azure provider must target shared_platform.subscription_id."
    }
  }
}

data "azurerm_application_insights" "platform" {
  count = var.shared_platform == null ? 0 : 1

  name                = var.shared_platform.application_insights_name
  resource_group_name = var.shared_platform.resource_group_name
}

data "azurerm_container_app_environment" "platform" {
  count = var.shared_platform == null ? 0 : 1

  name                = var.shared_platform.container_app_environment_name
  resource_group_name = var.shared_platform.resource_group_name
}
