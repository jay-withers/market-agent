# Both run a public quickstart image for now, so both are publicly reachable
# unauthenticated placeholder pages.
#
# Separate naming instances so each app gets its own workload name; project_name
# is left out because container apps cap at 32 characters.

module "naming_api" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.environment, "api"]
}

module "naming_dashboard" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.environment, "dashboard"]
}

# No location argument: container apps inherit the environment's region. Jobs,
# confusingly, do require it.
resource "azurerm_container_app" "api" {
  name                         = module.naming_api.container_app.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "api"
      image  = local.placeholder_app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = local.tags
}

resource "azurerm_container_app" "dashboard" {
  name                         = module.naming_dashboard.container_app.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "dashboard"
      image  = local.placeholder_app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = local.tags
}
