locals {
  # Public Microsoft quickstart images: there's no application code yet, and
  # nothing to pull from a registry. Replacing these with real images (ghcr.io)
  # means adding a `registry` block and a Key Vault-backed `secret` for the pull
  # token.
  placeholder_app_image = "mcr.microsoft.com/k8se/quickstart:latest"
  placeholder_job_image = "mcr.microsoft.com/k8se/quickstart-jobs:latest"

  # Smallest combination Container Apps accepts; memory must be 2 GiB per vCPU.
  container_cpu    = 0.25
  container_memory = "0.5Gi"

  # Wired up now, against placeholder images that ignore them, so bringing up
  # real containers is a pure image-reference change. None is a secret — real
  # secrets come from Key Vault at runtime.
  common_env = {
    AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.this.client_id
    KEY_VAULT_URI                         = azurerm_key_vault.this.vault_uri
    SQL_SERVER_FQDN                       = azurerm_mssql_server.this.fully_qualified_domain_name
    SQL_DATABASE_NAME                     = azurerm_mssql_database.this.name
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.this.connection_string
    ENVIRONMENT                           = var.environment
  }
}
