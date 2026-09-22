# Move dev onto the shared platform

Dev reads these existing resources in subscription
`02d69b18-478e-4355-ba6e-3cac5585d1e9`, resource group `rg-platform-dev`:

- `log-platform-dev`
- `appi-platform-dev`
- `cae-platform-dev` (Consumption workload profile)

They must exist before planning. Use that subscription for the Azure provider.
The platform owns their settings and lifecycle, including logging configuration,
retention, ingestion caps and workspace quota alerts. Application diagnostics and
the job-failure alert use the shared workspace. The app resource-group budget
does not include charges incurred in the platform resource group.

Stg/prd still create dedicated resources. The `moved` blocks preserve their
existing Terraform addresses when adding conditional resource counts.

## Existing dev deployment

Changing `container_app_environment_id` replaces both apps and all seven jobs.
The dashboard managed certificate and hostname binding also need replacement.
Expect downtime and new default app hostnames. The database, Key Vault and
workload identity remain application-owned.

1. Capture the currently running image tags and runtime environment settings
   before replacing workloads. Image and environment changes are ignored by
   Terraform after creation, so the Terraform defaults may be older than the
   deployed configuration. Pass the current immutable `marketagent_image_tag`
   and `dashboard_image_tag` into the migration plan and reconcile any runtime
   overrides, including authentication settings, before enabling traffic/jobs.
2. Review a saved dev plan. This configuration also removes the old dedicated
   workspace, Application Insights component, environment and workspace quota
   alert. Historical telemetry is not copied to the shared workspace. Export
   any history that must be kept, or explicitly retain the old telemetry
   resources outside this state before applying. Do not import the shared
   platform resources into this application's state.
3. Review and apply the migration plan. Terraform recreates the dashboard, adds
   its custom hostname without a certificate, then issues the environment's
   managed certificate. If hostname validation fails, update the dashboard
   CNAME to the new `dashboard_fqdn` output and the `asuid` TXT record to the
   recreated app's domain-verification ID, wait for DNS, and apply again. Check
   the API origin and CORS settings against the new app hostnames.
4. Complete the existing manual bind step, using the full
   `container_app_environment_id` output for
   `az containerapp hostname bind --environment`: the environment lives in a
   different resource group from the dashboard.
5. Verify API health, dashboard authentication, job execution, telemetry and
   `make logs-azure-history`. Confirm the shared environment sends logs to
   `log-platform-dev`; this application does not change its logging settings.

The Azure identity running Terraform needs read access to the shared resources,
permission to join the shared environment and permission to manage the app's
certificate within it. Workloads still use their existing managed identity for
Key Vault and PostgreSQL.
