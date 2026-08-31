output "resource_group_name" {
  description = "Name of the created resource group."
  value       = azurerm_resource_group.this.name
}

output "key_vault_name" {
  description = "Key Vault name, for populating secret values with `az keyvault secret set`."
  value       = azurerm_key_vault.this.name
}

output "identity_name" {
  description = "Name of the workload's managed identity — the principal named in the `CREATE USER ... FROM EXTERNAL PROVIDER` step that grants it database access."
  value       = azurerm_user_assigned_identity.this.name
}

output "sql_server_fqdn" {
  description = "Hostname of the Azure SQL server."
  value       = azurerm_mssql_server.this.fully_qualified_domain_name
}

output "sql_database_name" {
  description = "Name of the Azure SQL database."
  value       = azurerm_mssql_database.this.name
}

output "api_fqdn" {
  description = "Public hostname of the API container app."
  value       = azurerm_container_app.api.ingress[0].fqdn
}

output "dashboard_fqdn" {
  description = "Public hostname of the dashboard container app."
  value       = azurerm_container_app.dashboard.ingress[0].fqdn
}
