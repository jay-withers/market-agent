locals {
  # The container's Resource Manager id, which a role assignment needs and
  # `azurerm_storage_container` does not export: its `id` is the data-plane URL.
  database_container_scope = "${azurerm_storage_account.database.id}/blobServices/default/containers/${azurerm_storage_container.database.name}"

  # The one file the jobs and the API share. `make deploy` passes it to every
  # workload as DATABASE_BLOB_URL; the first writer creates it.
  database_blob_url = "${azurerm_storage_account.database.primary_blob_endpoint}${azurerm_storage_container.database.name}/marketagent.duckdb"
}
