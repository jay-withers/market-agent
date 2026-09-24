locals {
  # One image per application: the API, the agent and the summary job are three
  # entrypoints into the same Python package, differing only by the container's
  # `args`. The dashboard is genuinely separate and has its own.
  #
  # No `registry` block and no pull secret anywhere below, which is the whole
  # point of the packages being public.
  app_image       = "${var.image_registry}/marketagent:${var.marketagent_image_tag}"
  dashboard_image = "${var.image_registry}/dashboard:${var.dashboard_image_tag}"

  # uvicorn binds 8000; the dashboard's nginx runs unprivileged and so cannot
  # bind anything below 1024.
  api_target_port       = 8000
  dashboard_target_port = 8080

  # Smallest combination Container Apps accepts; memory must be 2 GiB per vCPU.
  container_cpu    = 0.25
  container_memory = "0.5Gi"

  # None of this is a secret — real secrets are read from Key Vault at runtime
  # by the workload identity, which is why Terraform owns no secret values and
  # no Key Vault reference.
  common_env = {
    AZURE_CLIENT_ID   = azurerm_user_assigned_identity.this.client_id
    KEY_VAULT_URI     = azurerm_key_vault.this.vault_uri
    POSTGRES_HOST     = azurerm_postgresql_flexible_server.this.fqdn
    POSTGRES_DATABASE = azurerm_postgresql_flexible_server_database.this.name
    POSTGRES_PORT     = "5432"
    # The database *role*, which is the managed identity's name — the server has
    # no password, so the username is the identity and the password is a token
    # minted per connection. Empty POSTGRES_PASSWORD is what selects that path.
    POSTGRES_USER                         = azurerm_user_assigned_identity.this.name
    APPLICATIONINSIGHTS_CONNECTION_STRING = local.application_insights_connection_string
    ENVIRONMENT                           = var.environment
    # The marketagent tag specifically, not a shared one: this is what the agent
    # records on its `agent_runs` row, so it has to name the image the code
    # writing that row is actually running. The dashboard never reads it.
    IMAGE_TAG = var.marketagent_image_tag
  }

  agent_base_env = merge(local.common_env, {
    DRY_RUN                 = tostring(var.agent_dry_run)
    ALPACA_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
  })

  # One entry per pot, each with its own agent job, sync job, Alpaca paper
  # account and DeepSeek key. The key must match a `portfolio` row (sql/011);
  # the app derives the pot's Key Vault secret names from it.
  #
  # `env` is where one pot differs from the others — ANALYSIS_EFFORT, a RISK_*
  # limit or MAX_RUN_COST_USD — merged over the agent's defaults. Every pot
  # runs the same settings today so the comparison is between sectors, and an
  # override lands only when the job is created: `env` is under ignore_changes,
  # so changing one later is `az containerapp job update --set-env-vars`.
  #
  # No MAX_RUN_COST_USD override: the app's $1.00 default is about 2.5x what a
  # 50-ticker pot costs even with news on every ticker (~$0.008 per analysis).
  pots = {
    tech   = { env = {} }
    health = { env = {} }
    energy = { env = {} }
  }

  agent_env = { for pot, cfg in local.pots : pot => merge(local.agent_base_env, cfg.env) }
}
