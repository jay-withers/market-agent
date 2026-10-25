environment = "dev"

shared_platform = {
  subscription_id                = "02d69b18-478e-4355-ba6e-3cac5585d1e9"
  resource_group_name            = "rg-platform-dev"
  log_analytics_workspace_name   = "log-platform-dev"
  application_insights_name      = "appi-platform-dev"
  container_app_environment_name = "cae-platform-dev"
}

dashboard_custom_domain_name = "marketagent.jaywithers.uk"

# Key Vault Secrets Officer: the same human as the PostgreSQL Entra admin in
# main.database.tf. Explicit, because CD applies as the pipeline identity.
key_vault_administrator_object_ids = ["b168eef0-d213-406e-a7f9-7b9198d580da"]
