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

# The two job names are the deploy targets: a scheduled job has to be started by
# hand to test it, with `az containerapp job start --name <this>`.
output "agent_job_name" {
  description = "Name of the agent container app job, for `az containerapp job start`."
  value       = azurerm_container_app_job.agent.name
}

output "summary_job_name" {
  description = "Name of the daily summary container app job, for `az containerapp job start`."
  value       = azurerm_container_app_job.daily_summary.name
}

output "identity_client_id" {
  description = "Client ID of the workload identity, which the containers receive as `AZURE_CLIENT_ID` and use to acquire Key Vault and PostgreSQL tokens."
  value       = azurerm_user_assigned_identity.this.client_id
}
