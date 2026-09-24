output "log_analytics_workspace_customer_id" {
  description = "Workspace customer ID consumed by make logs-azure-history."
  value       = local.log_analytics_workspace_customer_id
}

output "container_app_environment_id" {
  description = "Environment ARM ID used to bind the dashboard hostname across resource groups."
  value       = local.container_app_environment_id
}

output "container_app_log_environment_name" {
  description = "Generated environment identifier in EnvironmentName_s, used by make logs-azure-history."
  value       = local.container_app_log_environment_name
}

output "resource_group_name" {
  description = "Name of the created resource group."
  value       = azurerm_resource_group.this.name
}

output "key_vault_name" {
  description = "Key Vault name, for populating secret values with `az keyvault secret set`."
  value       = azurerm_key_vault.this.name
}

output "identity_name" {
  description = "Name of the workload's managed identity — the principal named in the `pgaadauth_create_principal` step that grants it database access."
  value       = azurerm_user_assigned_identity.this.name
}

output "postgres_fqdn" {
  description = "Hostname of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.this.fqdn
}

output "postgres_database_name" {
  description = "Name of the PostgreSQL database."
  value       = azurerm_postgresql_flexible_server_database.this.name
}

output "api_fqdn" {
  description = "Public hostname of the API container app."
  value       = azurerm_container_app.api.ingress[0].fqdn
}

output "dashboard_fqdn" {
  description = "Public hostname of the dashboard container app."
  value       = azurerm_container_app.dashboard.ingress[0].fqdn
}

output "dashboard_custom_domain_url" {
  description = "The dashboard's bound custom domain, if var.dashboard_custom_domain_name is set. Empty otherwise — the azurecontainerapps.io URL above always works regardless."
  value       = var.dashboard_custom_domain_name != "" ? "https://${var.dashboard_custom_domain_name}" : ""
}

# api_app_name and dashboard_app_name, alongside the three job names below, are
# what `make deploy` resolves before calling `az containerapp update` /
# `az containerapp job update` — the image and IMAGE_TAG on all five are
# lifecycle.ignore_changes'd, so that az cli step is the only thing that
# actually moves them.
output "api_app_name" {
  description = "Name of the API container app, for `az containerapp update`."
  value       = azurerm_container_app.api.name
}

output "dashboard_app_name" {
  description = "Name of the dashboard container app, for `az containerapp update`."
  value       = azurerm_container_app.dashboard.name
}

# The job names are the deploy targets: a scheduled job has to be started by
# hand to test it, with `az containerapp job start --name <this>`.
output "agent_job_names" {
  description = "Each pot's agent container app job name, keyed by pot, for `make deploy` and `az containerapp job start`."
  value       = { for pot, job in azurerm_container_app_job.agent : pot => job.name }
}

output "sync_job_names" {
  description = "Each pot's broker sync container app job name, keyed by pot, for `make deploy`."
  value       = { for pot, job in azurerm_container_app_job.sync : pot => job.name }
}

output "summary_job_name" {
  description = "Name of the daily summary container app job (covers every pot), for `az containerapp job start`."
  value       = azurerm_container_app_job.daily_summary.name
}

output "weekly_review_job_name" {
  description = "Name of the weekly review container app job (covers every pot), for `az containerapp job start`."
  value       = azurerm_container_app_job.weekly_review.name
}

output "identity_client_id" {
  description = "Client ID of the workload identity, which the containers receive as `AZURE_CLIENT_ID` and use to acquire Key Vault and PostgreSQL tokens."
  value       = azurerm_user_assigned_identity.this.client_id
}
