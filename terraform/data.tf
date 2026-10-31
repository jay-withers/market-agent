# Supplies tenant_id for the Key Vault and PostgreSQL server, and the
# subscription the shared-platform precondition checks.
data "azurerm_client_config" "current" {}

data "azurerm_log_analytics_workspace" "platform" {
  count = var.shared_platform == null ? 0 : 1

  name                = var.shared_platform.log_analytics_workspace_name
  resource_group_name = var.shared_platform.resource_group_name

  lifecycle {
    precondition {
      condition     = var.shared_platform.subscription_id == null || data.azurerm_client_config.current.subscription_id == var.shared_platform.subscription_id
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

# This project's resource group, created by github-repos along with the
# pipeline identity's grants on it. Same name the naming module always gave
# it. Everything here takes the group's region, so check it is the one
# var.location pins (github-repos' deploy_location decides it).
data "azurerm_resource_group" "this" {
  name = module.naming.resource_group.name

  lifecycle {
    postcondition {
      condition     = self.location == var.location
      error_message = "The resource group must be in var.location; see that variable for why only northeurope works."
    }
  }
}
