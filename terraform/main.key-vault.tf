# Terraform owns the vault and access to it, deliberately not the secret values:
# nothing here creates an azurerm_key_vault_secret. Application secrets
# (OPENAI_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY, NEWS_API_KEY,
# EMAIL_CREDENTIAL) are set with `az keyvault secret set`, so they never reach
# Terraform source or state.
resource "azurerm_key_vault" "this" {
  # checkov:skip=CKV_AZURE_42: purge protection deliberately off — see below.
  # checkov:skip=CKV_AZURE_110: same.
  # checkov:skip=CKV_AZURE_109: no network ACLs — see CKV_AZURE_189.
  # checkov:skip=CKV_AZURE_189: public access stays enabled. Restricting it needs a
  #   private endpoint (standing monthly cost, and there's no VNet — the Container Apps
  #   environment is Consumption-only) or a static egress IP the apps don't have.
  # checkov:skip=CKV2_AZURE_32: no private endpoint, as above.
  name                = module.naming.key_vault.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # Optional in azurerm 4.x, required in 5.x — set explicitly.
  rbac_authorization_enabled = true

  # Off, with the minimum retention: with purge protection on, `terraform
  # destroy` leaves a soft-deleted vault holding the name for up to 90 days,
  # which then blocks recreating it.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  public_network_access_enabled = true

  tags = local.tags
}

resource "azurerm_role_assignment" "key_vault_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.this.principal_id

  # Skips the provider's Entra lookup, which intermittently fails against a
  # just-created identity while the directory replicates.
  principal_type = "ServicePrincipal"
}

# Whoever runs `terraform apply` is always included, so a fresh deployment is
# immediately usable.
resource "azurerm_role_assignment" "key_vault_secrets_officer" {
  for_each = toset(concat(
    [data.azurerm_client_config.current.object_id],
    var.key_vault_administrator_object_ids,
  ))

  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = each.value
}
