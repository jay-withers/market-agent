environment = "dev"

shared_platform = {
  resource_group_name            = "rg-platform-dev"
  log_analytics_workspace_name   = "log-platform-dev"
  application_insights_name      = "appi-platform-dev"
  container_app_environment_name = "cae-platform-dev"
}

dashboard_custom_domain_name = "marketagent.jaywithers.uk"

# Pause requested 2026-11-24: once applied, no job fires on its schedule.
# Set false (or delete the line) to resume. Both directions replace the jobs
# with marketagent_image_tag; see docs/infrastructure.md#pausing.
paused = true
