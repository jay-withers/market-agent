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

  database_alerts = {
    # The one resource here that is always on, and the only one that can fail in
    # a way no job's own error handling can report: a job that cannot reach the
    # database may never get far enough to write down why.
    #
    # Known limit, stated rather than left to be discovered: a *stopped* server
    # emits no metric at all, and a metric alert cannot fire on absent data. So
    # this catches a server running and unhealthy, not one stopped — deliberately,
    # to dodge the £13/month, or by the 7-day auto-restart cycle. That case shows
    # up as three failed jobs instead, which the rules above do catch.
    db-down = {
      scope       = azurerm_postgresql_flexible_server.this.id
      description = "The PostgreSQL server stopped reporting itself alive."
      severity    = 0
      namespace   = "Microsoft.DBforPostgreSQL/flexibleServers"
      metric      = "is_db_alive"
      aggregation = "Maximum"
      operator    = "LessThan"
      threshold   = 1
      frequency   = "PT5M"
      window      = "PT15M"
      dimension   = null
    }

    # Storage on Flexible Server can only ever be grown, never shrunk, and
    # `auto_grow_enabled = false` means filling it is an outage rather than a
    # surprise bill. 80% on 32 GB leaves ~6 GB, which at this database's growth
    # rate is a long time to notice — the point is to hear about it while the fix
    # is still deleting rows rather than permanently raising the monthly floor.
    #
    # Hourly: storage moves slowly, and a tighter window would pay per evaluation
    # for a number that cannot change quickly.
    db-storage = {
      scope       = azurerm_postgresql_flexible_server.this.id
      description = "PostgreSQL storage is over 80% of a figure that cannot be reduced."
      severity    = 2
      namespace   = "Microsoft.DBforPostgreSQL/flexibleServers"
      metric      = "storage_percent"
      aggregation = "Average"
      operator    = "GreaterThan"
      threshold   = 80
      frequency   = "PT1H"
      window      = "PT1H"
      dimension   = null
    }
  }

  # Every metric alert as one table, because they differ only in what they watch.
  # The scope, action group, name and tags are identical on both and are
  # written once in `main.alerts.tf`; adding another rule is an entry here
  # rather than another twenty-five-line block to keep in step with the others.
  # Job failures used to live here too — see the comment above database_alerts
  # for why they moved to a log alert instead.
  metric_alerts = local.database_alerts
}
