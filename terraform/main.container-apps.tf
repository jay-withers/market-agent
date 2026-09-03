# Both are publicly reachable and unauthenticated. That is a deliberate,
# documented position rather than an oversight: the API is read-only, so this
# process cannot place a trade or alter a decision however it is called, and no
# response contains a secret, any PII, or real money. The proper fix is
# Container Apps EasyAuth with Entra, which `azurerm` does not expose and which
# would need `azapi`.
#
# Separate naming instances so each app carries its own workload name.
#
# ca-marketagent-dev-dashboard is 28 of the 32 characters container apps allow,
# which makes it the binding constraint on var.project_name's length.

module "naming_api" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "api"]
}

module "naming_dashboard" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "dashboard"]
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
      image  = local.app_image
      cpu    = local.container_cpu
      memory = local.container_memory

      # One image, three entrypoints: the console script picks the workload.
      command = ["investagent"]
      args    = ["api"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }

      # Liveness deliberately hits /healthz, which does **not** touch the
      # database. A failed liveness probe restarts the container, so a probe
      # that depended on PostgreSQL would turn a database blip into a
      # cluster-wide crash loop.
      liveness_probe {
        transport        = "HTTP"
        port             = local.api_target_port
        path             = "/healthz"
        initial_delay    = 5
        interval_seconds = 30
        timeout          = 5

        failure_count_threshold = 3
      }

      # Readiness is where the database check belongs: it takes a replica out
      # of rotation without killing it.
      readiness_probe {
        transport        = "HTTP"
        port             = local.api_target_port
        path             = "/readyz"
        interval_seconds = 10
        timeout          = 5

        failure_count_threshold = 3
        success_count_threshold = 1
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = local.api_target_port
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
      image  = local.dashboard_image
      cpu    = local.container_cpu
      memory = local.container_memory

      # API_ORIGIN and nothing else. The dashboard is nginx serving static
      # files: it holds no credential, touches no database, and passing it
      # `common_env` would put the database host and the vault URI into a
      # container that has no use for either.
      #
      # The browser talks to the API directly, so this has to be an address the
      # *browser* can reach — the API's public ingress, not an internal name.
      # The entrypoint renders it into config.json at start-up, which is what
      # lets one image serve every environment.
      env {
        name  = "API_ORIGIN"
        value = "https://${azurerm_container_app.api.ingress[0].fqdn}"
      }

      # nginx answers this from memory, without reaching the API. A dashboard
      # that restarted whenever the API blipped would be strictly worse than
      # one showing a stale error.
      liveness_probe {
        transport        = "HTTP"
        port             = local.dashboard_target_port
        path             = "/healthz"
        initial_delay    = 3
        interval_seconds = 30
        timeout          = 5

        failure_count_threshold = 3
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = local.dashboard_target_port
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = local.tags
}
