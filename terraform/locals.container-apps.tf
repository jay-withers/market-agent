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

  # Per-account cost ceiling and (for dynamic-500 only) a daily-trade bump.
  # Not exposed as Terraform variables — RISK_* and MAX_RUN_COST_USD were
  # never wired through Terraform even for the single-account job (see the
  # application's own docs: the cost ceiling's default "applies to the
  # deployed job and an override is an env var on the container"); these
  # locals are that same style of override.
  #
  # MAX_RUN_COST_USD is an informed estimate against the measured 10-ticker
  # baseline (~$0.18/run in ~4 minutes), extrapolated by relative watchlist
  # size — not a firm prediction, and worth revisiting after each account's
  # first real week of runs. This one genuinely does scale with watchlist
  # size: it bounds LLM spend, which scales with articles and tickers
  # analysed regardless of account size.
  #
  # RISK_MAX_DAILY_TRADES does NOT scale with watchlist size the same way,
  # and static-100 gets no override at all — both new Alpaca accounts hold
  # $500 real cash, not the $100,000 risklimits.py's own defaults are sized
  # for (see its docstring), so `max_concentration_pct`/`max_total_exposure_pct`
  # (percentage caps, which scale automatically) cap the account at roughly 3-4
  # open positions regardless of watchlist size — a 500-ticker watchlist means
  # more candidates to filter, not more room to trade. dynamic-500 gets a
  # modest bump to 15 (the single-account default, already raised 3 -> 6 -> 10
  # through real iteration, is 10) purely because five hundred tickers gives a
  # somewhat higher chance of several independent names clearing analysis on
  # the same newsy day — not because the account can afford more total
  # exposure, which it can't.
  agent100_env = merge(local.agent_base_env, {
    MAX_RUN_COST_USD = "5.00"
  })

  agent500_env = merge(local.agent_base_env, {
    RISK_MAX_DAILY_TRADES = "15"
    MAX_RUN_COST_USD      = "25.00"
  })
}
