locals {
  # The built-in Owner role's definition GUID, which is the same in every Azure
  # tenant. Hardcoded rather than looked up: an `azurerm_role_definition` data
  # source would make `plan` need directory reads for a constant.
  owner_role_definition_id = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"

  # `Azure/naming/azurerm` has no metric-alert or budget token, so these two are
  # the only names in the configuration built by hand. Assembled once, so the
  # "every resource name includes project_name" rule holds here by construction
  # rather than by three separate interpolations remembering to.
  alert_name_prefix = "${var.project_name}-${var.environment}"

  # Every metric alert as one table, because they differ only in what they watch.
  # The scope, action group, name and tags are identical on both and are
  # written once in `main.alerts.tf`; adding another rule is an entry here
  # rather than another twenty-five-line block to keep in step with the others.
  # Empty since the PostgreSQL server went: its two rules (db-down,
  # db-storage) were the last entries. Job failures live in the shared
  # environment's log alert, and the database blob has no metric worth an
  # alert of its own: a job that cannot reach it fails, and that alert fires.
  metric_alerts = {}
}
