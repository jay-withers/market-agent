# Supplies tenant_id for the Key Vault and SQL server, and object_id for the
# default Key Vault Secrets Officer and SQL administrator assignments.
data "azurerm_client_config" "current" {}
