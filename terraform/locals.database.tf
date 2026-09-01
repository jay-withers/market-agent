locals {
  # The server has no password, so an Entra administrator is the only way in.
  # Defaulting to the deploying principal means a fresh deployment is usable
  # immediately, with no object ID to look up.
  #
  # Applied from CI, that administrator becomes the OIDC service principal and no
  # human can connect — set var.postgres_admin_object_id (an Entra group is
  # tidiest) and the matching principal type before letting CI apply.
  postgres_admin_object_id      = coalesce(var.postgres_admin_object_id, data.azurerm_client_config.current.object_id)
  postgres_admin_principal_name = coalesce(var.postgres_admin_principal_name, "terraform-deployer")
}
