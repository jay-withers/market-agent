# Where the database lives: one DuckDB file, `marketagent.duckdb`, in a private
# container. See apps/marketagent/src/marketagent/db.py for how the jobs and the
# API share it (a blob lease per write, an ETag-checked copy for reads).
#
# This replaced the PostgreSQL Flexible Server in main.database.tf, which billed
# its B1ms every hour for a database touched for minutes a day. The server
# stays until the jobs have run on the blob for a few days and the data has
# been checked, then a follow-up removes it.
#
# **`prevent_destroy` and a delete lock**, as gym-log's training log has: this
# is the experiment's whole record, and nothing rebuilds it.
resource "azurerm_storage_account" "database" {
  # checkov:skip=CKV_AZURE_206: LRS on purpose. LRS is already eleven nines of
  #   durability within the region; the realistic risk to this file is a bad
  #   write or an accidental delete, which versioning and the retention policies
  #   below cover and which geo-redundancy would not.
  # checkov:skip=CKV_AZURE_59: public network access stays enabled, for the same
  #   reason as the Key Vault — a scale-to-zero container app on a
  #   Consumption-only shared environment has neither a VNet to peer nor a
  #   static egress IP to allow.
  # checkov:skip=CKV_AZURE_33: no queues are used, so queue logging has nothing
  #   to log.
  # checkov:skip=CKV2_AZURE_1: platform-managed keys. A CMK needs a second Key
  #   Vault key, rotation and a managed identity grant on it, to protect a
  #   paper-trading record with no real money or PII in it.
  # checkov:skip=CKV2_AZURE_33: no private endpoint, as above.
  # checkov:skip=CKV2_AZURE_47: public access as above.
  name                = module.naming.storage_account.name
  resource_group_name = data.azurerm_resource_group.this.name
  location            = data.azurerm_resource_group.this.location

  account_tier             = "Standard"
  account_kind             = "StorageV2"
  account_replication_type = "LRS"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  public_network_access           = "Enabled"

  # Entra only: the identity and the operators reach it through role
  # assignments, so there is no account key to leak.
  shared_access_key_enabled = false

  blob_properties {
    # Every upload is a new version, so a bad write is undone by promoting the
    # previous one. A file of a few MB written a few dozen times a day is
    # pennies a month in versions.
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = local.tags
}

resource "azurerm_management_lock" "database" {
  name       = "lock-${azurerm_storage_account.database.name}"
  scope      = azurerm_storage_account.database.id
  lock_level = "CanNotDelete"
  notes      = "MarketAgent's database: every run, decision and trade, which cannot be rebuilt. Remove with github-repos' manual-remove-lock."
}

resource "azurerm_storage_container" "database" {
  # checkov:skip=CKV2_AZURE_21: no blob read logging. It would land in the shared
  #   Log Analytics workspace, whose `daily_quota_gb = 0.15` is split across every
  #   tenant on the platform — spent on recording that a page load read one file.
  name                  = "database"
  storage_account_id    = azurerm_storage_account.database.id
  container_access_type = "private"

  # As well as the account's: this repo's tflint asks it of anything holding
  # data, and replacing the container would empty it just the same.
  lifecycle {
    prevent_destroy = true
  }
}

# The jobs write the database and the API reads it, all as this identity.
#
# Scoped to the container rather than the account: it is the only thing the
# identity needs here. The scope is assembled by hand (local.database_container_scope)
# because `azurerm_storage_container` exports no Resource Manager id.
resource "azurerm_role_assignment" "identity_database_contributor" {
  scope                = local.database_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  # Stated explicitly: without it the provider looks the principal up in the
  # directory, which fails intermittently on an identity created moments ago.
  principal_type = "ServicePrincipal"
}

# The operators, who already populate Key Vault, also run `make import-pg` and
# `make sql` locally against the real blob, as gym-log's do with its log.
resource "azurerm_role_assignment" "operator_database_contributor" {
  for_each = toset(var.key_vault_administrator_object_ids)

  scope                = local.database_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
  principal_type       = "User"
  description          = "Imports and edits the database by hand (make import-pg, make sql)"
}
