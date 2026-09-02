# PostgreSQL Flexible Server, not Azure SQL — forced, not chosen. This
# subscription is blocked from provisioning Azure SQL in every region the
# `allowed-locations-dev` policy permits (westeurope and northeurope), and the
# block is regional rather than per-SKU: all 208 service objectives across all
# ten editions report `Visible` rather than `Available`, Basic and Free
# included. PostgreSQL is restricted in westeurope but open in northeurope,
# which is why var.location defaults there. See the README before changing
# either.
#
# The cost consequence is real: Flexible Server has no serverless or auto-pause
# tier, so this bills its SKU per hour for as long as it exists — roughly
# £13/month on B1ms rather than the near-zero of an auto-pausing Azure SQL
# database. There is no configuration that avoids that; only stopping the server
# does.
#
# Consequence for application code: psycopg, not pyodbc/aioodbc.

resource "azurerm_postgresql_flexible_server" "this" {
  # checkov:skip=CKV_AZURE_136: geo-redundant backup doubles backup storage cost for a
  #   database holding no real money or personal data, and cannot be changed after
  #   creation — see geo_redundant_backup_enabled below.
  # checkov:skip=CKV2_AZURE_57: no private endpoint. It is a standing cost and there's no
  #   VNet to attach it to — the Container Apps environment is Consumption-only. Access is
  #   controlled by Entra-only auth plus the firewall rule below.
  name                = module.naming.postgresql_server.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  version             = var.postgres_version

  sku_name = var.postgres_sku_name

  # Storage can never be reduced, only grown, and it bills the provisioned
  # figure rather than bytes used. auto_grow_enabled is therefore off on
  # purpose: left on, a runaway table silently and permanently raises the floor.
  storage_mb        = var.postgres_storage_mb
  storage_tier      = var.postgres_storage_tier
  auto_grow_enabled = false

  # Seven days is the minimum. Geo-redundant backup is a second copy at extra
  # cost and is immutable after creation, so it is set explicitly rather than
  # left to the provider default.
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  # No high_availability block: a standby replica doubles the compute bill to
  # buy availability this experiment doesn't need.

  public_network_access_enabled = false

  # Entra-only, matching the passwordless posture everywhere else: with
  # password_auth_enabled false, administrator_login and administrator_password
  # stay unset and no database password exists in source, state or Key Vault.
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  zone = var.postgres_zone

  tags = local.tags

  lifecycle {
    # Azure fills these in itself when omitted, and reports values that differ
    # from an empty config on the next plan.
    ignore_changes = [zone]
  }
}

# Unlike Azure SQL's inline azuread_administrator block, the Entra admin is a
# separate resource here — and it needs the principal's *type*, which
# data.azurerm_client_config cannot report. Hardcoded rather than derived from the
# deploying principal, so a CI apply doesn't make the OIDC service principal the
# administrator and lock every human out.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "this" {
  server_name         = azurerm_postgresql_flexible_server.this.name
  resource_group_name = azurerm_resource_group.this.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = "b168eef0-d213-406e-a7f9-7b9198d580da"
  principal_name      = "Jay Withers"
  principal_type      = "User"
}

resource "azurerm_postgresql_flexible_server_database" "this" {
  name      = module.naming.postgresql_database.name
  server_id = azurerm_postgresql_flexible_server.this.id

  # UTF8 with a deterministic sort order. Both force replacement of the
  # database if changed.
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# 0.0.0.0-0.0.0.0 is Azure's sentinel for "allow other Azure services", which is
# what lets the container apps reach the database without a VNet. It does not
# cover developer machines — see the README.
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure_services" {
  # checkov:skip=CKV2_AZURE_26: the sentinel described above grants reachability, not
  #   access: the server is Entra-only with no password.
  name             = "AllowAllAzureIps"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}
