# Azure SQL serverless rather than PostgreSQL Flexible Server, which bills per
# hour for as long as it exists and has no auto-pause tier. Consequence for
# application code: an ODBC driver (pyodbc/aioodbc), not psycopg.
#
# Compute is billed per vCore-second only while online, and the database
# auto-pauses once idle. A long-lived connection pool keeps sessions open and
# stops it ever pausing — see the README.

resource "azurerm_mssql_server" "this" {
  # checkov:skip=CKV_AZURE_23: no extended auditing — it needs a storage account or Log
  #   Analytics sink, i.e. standing cost, for a database holding no real money or
  #   personal data.
  # checkov:skip=CKV_AZURE_24: 90-day audit retention, as above.
  # checkov:skip=CKV_AZURE_113: public access stays enabled. A private endpoint is a
  #   standing cost and there's no VNet to attach it to — the Container Apps environment
  #   is Consumption-only. Access is controlled by Entra-only auth plus the rule below.
  # checkov:skip=CKV2_AZURE_45: no private endpoint, as above.
  # checkov:skip=CKV2_AZURE_2: no Defender for SQL — a per-server monthly charge.
  name                = module.naming.mssql_server.name_unique
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  version             = "12.0"

  minimum_tls_version           = "1.2"
  public_network_access_enabled = true

  # Declaring azuread_authentication_only satisfies the provider's requirement
  # for an administrator, so administrator_login and administrator_login_password
  # stay unset: there is no SQL password in source, state, or Key Vault.
  #
  # The workload identity gets no database access here — a contained user for a
  # managed identity needs `CREATE USER ... FROM EXTERNAL PROVIDER`, T-SQL that
  # Terraform cannot execute. See the README for that step.
  azuread_administrator {
    login_username              = local.sql_admin_login_username
    object_id                   = local.sql_admin_object_id
    tenant_id                   = data.azurerm_client_config.current.tenant_id
    azuread_authentication_only = true
  }

  tags = local.tags
}

resource "azurerm_mssql_database" "this" {
  # checkov:skip=CKV_AZURE_224: ledger tables are irreversible once enabled and add write
  #   overhead. Auditability comes from the ai_decisions table, not cryptographic proof.
  # checkov:skip=CKV_AZURE_229: zone redundancy raises the per-second compute rate to buy
  #   high availability this experiment doesn't need.
  name      = module.naming.mssql_database.name_unique
  server_id = azurerm_mssql_server.this.id

  # min_capacity and auto_pause_delay_in_minutes are only valid on a GP_S_/HS_S_
  # SKU, and license_type is rejected outright on serverless.
  sku_name                    = "GP_S_Gen5_1"
  min_capacity                = 0.5
  auto_pause_delay_in_minutes = var.sql_auto_pause_delay_in_minutes

  # Billed on the provisioned maximum, not bytes used, so pinned rather than
  # left to its 32 GB default.
  max_size_gb = var.sql_database_max_size_gb

  zone_redundant = false

  # The default is "Geo" (read-access geo-redundant), which costs appreciably
  # more. geo_backup_enabled is ignored on this SKU, so this is the argument that
  # matters.
  storage_account_type = "Local"

  transparent_data_encryption_enabled = true

  # Do not add long_term_retention_policy here, or an
  # azurerm_mssql_server_dns_alias: both silently prevent auto-pause, turning
  # this into a 24/7 billed database with no obvious change in the plan.
  short_term_retention_policy {
    retention_days           = 1
    backup_interval_in_hours = 24
  }

  tags = local.tags
}

# 0.0.0.0-0.0.0.0 is Azure's sentinel for "allow other Azure services", which is
# what lets the Container Apps reach the database without a VNet. It does not
# cover developer machines — see the README.
resource "azurerm_mssql_firewall_rule" "allow_azure_services" {
  # checkov:skip=CKV2_AZURE_34: the sentinel described above grants reachability, not
  #   access: the server is Entra-only with no password.
  name             = "AllowAllWindowsAzureIps"
  server_id        = azurerm_mssql_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
