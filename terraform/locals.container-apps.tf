locals {
  # One image per application: the API, the agent and the summary job are three
  # entrypoints into the same Python package, differing only by the container's
  # `args`. The dashboard is genuinely separate and has its own.
  #
  # No `registry` block and no pull secret anywhere below, which is the whole
  # point of the packages being public.
  app_image       = "${var.image_registry}/investagent:${var.image_tag}"
  dashboard_image = "${var.image_registry}/dashboard:${var.image_tag}"

  # uvicorn binds 8000; the dashboard's nginx runs unprivileged and so cannot
  # bind anything below 1024.
  api_target_port       = 8000
  dashboard_target_port = 8080

  # Smallest combination Container Apps accepts; memory must be 2 GiB per vCPU.
  container_cpu    = 0.25
  container_memory = "0.5Gi"

  # None of this is a secret — real secrets are read from Key Vault at runtime
  # by the workload identity, which is why Terraform owns no secret values and
  # no Key Vault reference.
  common_env = {
    AZURE_CLIENT_ID   = azurerm_user_assigned_identity.this.client_id
    KEY_VAULT_URI     = azurerm_key_vault.this.vault_uri
    POSTGRES_HOST     = azurerm_postgresql_flexible_server.this.fqdn
    POSTGRES_DATABASE = azurerm_postgresql_flexible_server_database.this.name
    POSTGRES_PORT     = "5432"
    # The database *role*, which is the managed identity's name — the server has
    # no password, so the username is the identity and the password is a token
    # minted per connection. Empty POSTGRES_PASSWORD is what selects that path.
    POSTGRES_USER                         = azurerm_user_assigned_identity.this.name
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.this.connection_string
    ENVIRONMENT                           = var.environment
    IMAGE_TAG                             = var.image_tag
  }
}
