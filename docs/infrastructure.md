# Infrastructure

The Terraform root configuration: what it creates and why it is shaped by
cost, the database region trap, PostgreSQL, naming, security, running SQL by
hand, Checkov, monitoring, state, environments and schedules. Moved out of
CLAUDE.md, which keeps the rules and links here.

## Terraform layout

`terraform/` is a **deployable root configuration**, not a reusable module: it has
its own `provider` block in `versions.tf`, and `.terraform.lock.hcl` is committed.
There is no `examples/` directory (an earlier version had one purely so a
provider-less module could be planned) and there are no tests. The Terraform
version is pinned in `.terraform-version` at the repo root, which is where
tfenv/tenv and CI look for it.

**File layout is enforced, not just conventional**: `locals`/`variable`/`output`/
`data` blocks must live in a matching `locals.tf`/`variables.tf`/`outputs.tf`/
`data.tf`, and `terraform{}`/`provider{}` blocks in `versions.tf`, or a
topic-scoped variant of any of them (`main.database.tf`, `locals.container-apps.tf`,
`variables.optional.tf`), via the local pre-commit hook
`scripts/check-tf-standards.sh`. TFLint's `terraform_standard_module_structure`
rule covers similar ground but hardcodes the exact filenames `variables.tf`/
`outputs.tf` with no topic-scoped support and no locals/data/versions coverage, so
it's deliberately left out of the enabled ruleset in favour of the custom script.
That script is shared verbatim with `terraform-root-aks`, `azure-landingzone` and
`github-repos`; a change here should be re-copied there rather than diverging.

Two repo-specific conventions on top of that, both requested explicitly:

- **Variables are split by whether they must be supplied**: `variables.required.tf`
  (only `environment`) and `variables.optional.tf` (everything with a default).
- **Comments and outputs earn their place.** Comment the non-obvious — a cost
  trade-off, a provider quirk, a trap — not what the code already says. Same for
  outputs: add one because something consumes it, not for completeness.

## What this configuration creates

Dev uses the existing `log-platform-dev`, `appi-platform-dev` and
`cae-platform-dev` in `rg-platform-dev`, selected by `shared_platform` in
`terraform/environments/dev.tfvars`. These are data sources; workspace quotas
and retention belong to the platform. Stg/prd still create dedicated resources.
See `docs/shared-platform-migration.md` before applying to an existing dev stack.

Resource group (read by data source: github-repos creates it), user-assigned managed identity, Log Analytics workspace,
Application Insights (workspace-based), Key Vault (RBAC), Container Apps
environment (Consumption-only), two container apps (`api`, `dashboard`),
scheduled container app jobs — an `agent-<pot>` and a `sync-<pot>` per pot in
`local.pots`, plus `summary` and `weekly` covering every pot — a
PostgreSQL Flexible Server plus database, and the alerting in `main.alerts.tf`
(action group, five metric alerts, one log alert, a budget, two diagnostic
settings). Names come from `Azure/naming/azurerm`.

### The cost constraint drives most of the design

The hard requirement is that **nothing bills meaningfully while idle**. That's
why:

- **There is no Azure Container Registry.** ACR Basic is a flat monthly charge
  with no consumption tier; images will live in ghcr.io. Don't add ACR back
  without raising the cost trade-off.
- **The database is PostgreSQL Flexible Server, and that was forced, not chosen.**
  Azure SQL serverless is the better fit on cost — it auto-pauses to near zero —
  but this subscription cannot provision Azure SQL in any region the
  `allowed-locations-dev` policy permits. See "The database and the region trap"
  below before assuming this is a preference that can be revisited. The
  consequence for future application code is `psycopg`, not `pyodbc`/`aioodbc`;
  the relational schema from the project spec is unaffected.
- **Container apps use `min_replicas = 0`** and the environment has no
  `workload_profile` block. Adding a workload profile introduces a standing
  per-hour charge.
- **`daily_quota_gb = 0.15`** on the Log Analytics workspace, and
  `daily_data_cap_in_gb = 0.1` on Application Insights. Log ingestion is
  otherwise the largest cost risk: Azure Monitor's free grant is 5 GB/month, and
  the default App Insights cap is 100 GB/day.
- **`storage_mb` sits at Azure's 32 GB minimum** with `auto_grow_enabled = false`.
  Storage bills the provisioned figure rather than bytes used, and Flexible Server
  storage **can never be reduced, only grown** — auto-grow left on would let a
  runaway table permanently raise the floor.

The honest summary of the cost position: the "nothing bills while idle" rule now
holds for everything *except* the database, which bills roughly **£13/month**
(B1ms compute ~£9.71 plus ~£3 storage, North Europe list) for as long as it
exists. Flexible Server has no serverless or auto-pause tier — that is not a
configuration gap, the SKU does not exist. Only stopping the server avoids it, and
a stopped server auto-restarts after 7 days.

One upside worth noting: the old Azure SQL design carried a £150/month
connection-pool trap, where a long-lived pool prevented auto-pause. With an
always-on server that trap is simply gone. Pool freely.

### The database and the region trap

This is the most surprising thing in the repo, and it is worth reading before
touching either the database or `var.location`.

**Azure SQL cannot be provisioned by this subscription in any permitted region.**
Two independent constraints intersect:

- An enforced Azure Policy assignment, `allowed-locations-dev`, with effect
  **Deny** and `listOfAllowedLocations = ["westeurope", "northeurope"]`. No
  `notScopes`, no exemptions.
- The subscription is separately restricted from provisioning Azure SQL in both of
  those regions.

The restriction is **regional, not per-SKU** — worth stating because the obvious
first instinct is to try a smaller SKU. In westeurope and northeurope, all 208
service objectives across all ten editions report `status: Visible` rather than
`Available`, `Free` and `Basic` included. For contrast, francecentral returns 129
`Available` + 9 `Default` + 29 `Visible`, which shows `Visible` is a meaningful
per-SKU signal when a region is actually open. There is no SKU that rescues it.

PostgreSQL Flexible Server is restricted in westeurope but **open in northeurope**,
which is the only reason a database can be deployed at all. Hence
`var.location = "northeurope"`.

Probe before changing any of this; the provider's advertised location list is not
subscription-aware, but the capabilities APIs are:

```bash
SUB=$(az account show --query id -o tsv)
# Azure SQL: want status "Available", not "Visible"
az rest --method get --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.Sql/locations/<region>/capabilities?api-version=2023-08-01" --query "{status:status,reason:reason}"
# PostgreSQL: want restricted "Disabled"
az rest --method get --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.DBforPostgreSQL/locations/<region>/capabilities?api-version=2023-06-01-preview" --query "value[0].{restricted:restricted,reason:reason}"
```

**If the £13/month becomes annoying**, the fix is not a Terraform change — it is
amending `allowed-locations-dev` to admit a SQL-capable region (francecentral,
swedencentral, germanywestcentral, switzerlandnorth, italynorth and spaincentral
were all verified `Available`), then moving back to Azure SQL. That policy looks
like it belongs to the `azure-landingzone` repo, so the change belongs there
rather than by hand in the portal. Azure SQL's free offer (100,000 vCore-seconds
and 32 GB monthly, permanently) would then make the database genuinely free, but
it needs `azapi_resource` — `azurerm` exposes no `useFreeLimit` in any version,
re-verify with `terraform providers schema -json` before assuming otherwise. A
working `azapi` implementation of exactly that was written and then reverted when
the policy came to light; it is in the git history of this branch if it is ever
needed again.

Also worth knowing: `Microsoft.App` needed explicit registration on this
subscription (`az provider register --namespace Microsoft.App`). A fresh
subscription fails the container apps environment with
`MissingSubscriptionRegistration` until that is done.

### PostgreSQL specifics

- The Entra administrator is a **separate resource**
  (`azurerm_postgresql_flexible_server_active_directory_administrator`), not an
  inline block as it was on Azure SQL, and it requires the principal's *type*
  explicitly — `data.azurerm_client_config` cannot report it. `object_id` and
  `principal_name` come from `var.postgres_entra_administrator`, a **named
  human**, and `principal_type` is fixed to `User`: the administrator is then
  the same principal whoever or whatever applies. The value isn't committed:
  CI reads `TF_VAR_POSTGRES_ENTRA_ADMINISTRATOR` from the `dev` environment,
  where github-repos sets it from its `var.operator`.
- **`principal_name` must be the UPN, not the display name** — ARM makes it the
  PostgreSQL role name verbatim. Registered as `Jay Withers`, the role really is
  called `Jay Withers`, and connecting as the UPN fails with
  `password authentication failed`, which points at the token rather than the
  missing role and makes this hard to diagnose. Confirmed both ways: the display
  name broke the login, the UPN fixed it. Note the `az` flag for this is called
  `--display-name`, which is how the trap gets set. Read what is actually
  registered with:

  ```bash
  SUB=$(az account show --query id -o tsv)
  az rest --method get --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/rg-marketagent-dev/providers/Microsoft.DBforPostgreSQL/flexibleServers/psql-marketagent-dev/administrators?api-version=2023-06-01-preview" --query "value[].properties.principalName"
  ```
- `authentication { password_auth_enabled = false }` keeps the passwordless
  posture: `administrator_login`/`administrator_password` stay unset and no
  database password exists in source, state or Key Vault.
- `geo_redundant_backup_enabled` **cannot be changed after creation**, so it is set
  explicitly rather than left to the provider default.
- `zone` is under `ignore_changes`: Azure assigns one when omitted and then reports
  a value that differs from an empty config on the next plan.
- The naming module has no flexible-server token; `postgresql_server` (slug `psql`)
  and `postgresql_database` (slug `psqldb`) are the right ones. There is no
  `postgresql_flexible_server`.

### Naming

Two requirements from the user drive this: names use the naming module's
**`.name`, not `.name_unique`** (deterministic, no random suffix), and
**every resource name includes `project_name`**. Verified names at
`project_name = "marketagent"`, `environment = "dev"`:

```
rg-marketagent-dev              cae-marketagent-dev   ca-marketagent-dev-api        (22/32)
kv-marketagent-dev      (18/24) log-marketagent-dev   ca-marketagent-dev-dashboard  (28/32)
psql-marketagent-dev            appi-marketagent-dev  caj-marketagent-dev-agent-health (32/32)
psqldb-marketagent-dev          uai-marketagent-dev   caj-marketagent-dev-sync-health  (31/32)
                                                      caj-marketagent-dev-summary   (27/32)
                                                      caj-marketagent-dev-weekly    (26/32)
```

`agent-<pot>` is what sets the six-character limit on pot names: `health` fills
all 32. A `precondition` on the agent job and `accounts.secret_suffix()` both
refuse a longer name, since the naming module would truncate it silently and
the app derives the pot's secret names from the same string.

The daily summary job is named `summary`, not `daily-summary`: the latter would be
33 characters against the 32 container app jobs allow, and the naming module
truncates silently. Its container is still `daily-summary`. The weekly review
hits the same wall — `weekly-review` would be 33 too — so the job is `weekly`
and its container is `weekly-review`.

Two consequences of dropping the suffix:

- **Key Vault and PostgreSQL server names are globally unique across Azure**, so an
  apply can fail on a name someone else already holds — or on one *you* previously
  soft-deleted. The fix is to change `project_name`, not to reintroduce
  `.name_unique`. (`.name_unique` was tried and reverted: its suffix comes from
  `module.naming.random_string.main` in state, so it is stable rather than random,
  and it reproduced the exact name of an already soft-deleted vault. It also moves
  the binding length constraint to Key Vault's 24, where a 15-character
  `project_name` silently truncates the suffix to one character and defeats the
  point.)
- A destroyed-and-recreated Key Vault reuses its name, which the soft-delete window
  can hold. `purge_soft_delete_on_destroy = true` in the provider `features` block
  is what keeps that from blocking recreation. Note that
  `recover_soft_deleted_key_vaults` is left at its default of **true**: a name
  matching a soft-deleted vault is *recovered* rather than created, and it comes
  back in its original region — which fails if `var.location` has changed since.

`project_name` is length-validated to 15 characters. The binding constraint is
`ca-<project>-<env>-dashboard` against the 32 characters container apps allow —
not, as you might expect, Key Vault's 24. The four container workloads have their
own naming module instances only so each carries its workload name
(`suffix = [var.project_name, var.environment, "<workload>"]`). Check with
`terraform console` before changing any of it — note `console` needs a real
backend init, unlike `validate`.

Also: there is no `postgresql_flexible_server` token — the flexible server uses
`postgresql_server` (slug `psql`) and its database `postgresql_database` (slug
`psqldb`). The module doesn't lowercase its dashed names, and
`postgresql_server`, `container_app` and `container_app_job` all reject
uppercase; job names also reject underscores.

### Security posture

Passwordless throughout. The PostgreSQL server sets
`authentication { active_directory_auth_enabled = true, password_auth_enabled = false }`,
which lets `administrator_login`/`administrator_password` stay unset — no database
password exists in source, state or Key Vault. The administrator is a named human
set by `var.postgres_entra_administrator` (object ID and principal name), so a CI
apply no longer silently makes the OIDC service principal the only way in — the
trap that an earlier "default to whoever applies" version carried. An Entra group
would survive personnel change, at the cost of `principal_type = "Group"` and a
group created out of band.

Terraform owns the Key Vault and its RBAC but creates **no**
`azurerm_key_vault_secret`: values are set out of band with
`az keyvault secret set` by whoever holds `Key Vault Secrets Officer` (exactly
`key_vault_administrator_object_ids` in the environment's tfvars, never the
deploying principal: cd-terraform applies as the pipeline identity, which
would move the grant to itself on every run).
The workload identity gets read-only `Key Vault Secrets User`. Never add secret
values to Terraform or tfvars.

Granting the managed identity database access needs
`SELECT pgaadauth_create_principal(...)`, SQL that the `azurerm` provider cannot
execute. **It has been run** — `uai-marketagent-dev` exists as a role with
`CONNECT`, `USAGE, CREATE ON SCHEMA public` and default privileges on future
tables and sequences. Re-running is harmless. The alternative, if it ever becomes
annoying, is an Entra security group as server administrator with the identity as
a member (adds the `azuread` provider and needs directory permissions).

### Running SQL by hand

`sql/` at the repository root holds one self-contained file per task, executed by
`scripts/Invoke-DbSql.ps1`. The split is deliberate: each `.sql` file names its
own identities and databases literally, so it runs under plain `psql --file`, and
the script is connection plumbing that knows nothing about what it runs.

Schema changes are numbered migrations (`001-schema.sql`) written entirely as
`CREATE ... IF NOT EXISTS`, and each inserts its own row into `schema_migrations`
at the end — the runner stays ignorant of what it ran. Chosen over Alembic
because the pattern already existed and the schema changes rarely; the honest
cost is no down-migrations and no ordering guarantee beyond the filename.

**`demo/seed.sql` is deliberately not in `sql/`**, and that is the whole point
of it living in its own directory: `make sql` with no arguments runs the
*entire* `sql/` directory against **Azure**, so a destructive fixture file
there would eventually be applied to the real database by someone bringing the
schema up to date. It is loaded only by `make demo`, and it opens with a guard
that raises unless `current_database()` is the local `marketagent`.
Prices are a deterministic pseudo-random walk hashed from the ticker and the
day index rather than `random()`, so the same seed always draws the same chart
and a screenshot means something.

- **`pgaadauth` exists only in the `postgres` maintenance database.** The
  application database has `plpgsql` and nothing else, so
  `pgaadauth_create_principal` fails there with `function ... does not exist` —
  which reads like a syntax or type problem rather than a wrong-database one.
  Roles are cluster-wide, so `grant-uai-access.sql` does the create after
  `\connect postgres`, then `\connect psqldb-marketagent-dev` for the GRANTs.
  `\connect` mid-file reuses the same token, so no re-authentication is needed.
- **`-Path` takes files and/or directories**, and defaults to the whole of `sql/`
  — a directory expands to its `*.sql` sorted by name, which is what the numeric
  prefixes are for. Everything in one invocation runs under **one** firewall rule
  and **one** access token; batching is what makes a migration run tolerable,
  given the serialisation trap below. `-SqlFile` survives as an alias.
- Each file gets **its own `psql` process** rather than all of them being
  concatenated: files are allowed to `\connect` elsewhere (`grant-uai-access.sql`
  has to), and `ON_ERROR_STOP` only aborts the process it is set on, so one
  process would let a later file run against the wrong database or after a
  failure.
- `ALTER DEFAULT PRIVILEGES` only ever affects objects created *afterwards*, so it
  cannot rescue a migration that ran before it. `grant-uai-access.sql` therefore
  also carries `GRANT ... ON ALL TABLES/SEQUENCES IN SCHEMA public`, which makes
  it safe to run at any point in the sequence rather than only first.
- The script hardcodes the dev resource names as parameter defaults. That is safe
  precisely because the naming convention has no random suffix, and it keeps
  Terraform, the state backend and git out of the path entirely.
- **Firewall rules require public access.** With
  `public_network_access_enabled = false` the API refuses every firewall
  operation, `list` included, and with no VNet or private endpoint the server is
  then unreachable by anything — the container apps included, since the
  "allow Azure services" sentinel is itself a firewall rule.
- The rule is named after the caller's public IP and reused if present; removal
  is prompted rather than automatic (`-KeepFirewallRule`, assumed when stdin is
  redirected). Firewall changes serialise on the server, so a second one while
  the first is still processing fails with `ServerIsBusy` — which is why a kept
  rule makes a retry loop much quicker.
- `$PSNativeCommandUseErrorActionPreference` is **off by default even in
  PowerShell 7.6**, so the script opts in explicitly; without it a failing `az`
  or `psql` is silently ignored.

Deliberately destroyable: `purge_protection_enabled = false` on the Key Vault and
`prevent_deletion_if_contains_resources = false` on the provider, both so
`terraform destroy` actually works. That's also why `.tflint.hcl` excludes
`azurerm_key_vault`, `azurerm_postgresql_flexible_server` and
`azurerm_postgresql_flexible_server_database` from
`azurerm_resources_missing_prevent_destroy` (verified: those exact three fire) —
excluded rather than disabling the rule, so it stays armed for anything added
later.

### Checkov skips

13 checks fire and every one is suppressed inline with a reason; there are no
global skips and no unnecessary ones (verified by stripping the comments and
re-running — that check caught one skip, `CKV_AZURE_130`, that did not actually
fire). They fall into three groups: features that need standing cost
(private endpoints, geo-redundant backup), the destroyability choices above, and
`CKV_TF_1` on each `Azure/naming/azurerm` instance (a Registry module pinned by
semver has no commit hash to pin). If you add a resource, run `checkov` and add
skips for exactly what fires — no more.

Exactly three fire on the database: `CKV_AZURE_136` (geo-redundant backup) and
`CKV2_AZURE_57` (private endpoint) on the server, `CKV2_AZURE_26` on the firewall
rule.

## Monitoring and alerting

Two layers, and the split matters: the email reports what the application knows
about itself, and the Azure alerts cover what it cannot know because it was
never alive to find out.

**The daily email states whether the agent ran, before anything it did.** This
is the gap the whole `_run_section`/`_run_alert` pair exists to close:
`No trades were made.` is the same sentence on a day the model held everything
and on a day the 10:30 job died, and the trades, decisions and holdings sections
are silent in exactly the same way, so nothing further down the email can tell
the two apart. `day_activity` therefore returns `runs` alongside the rest, and
the section renders before the trades table.

- **A bad run also reaches the subject line** — `no agent run`, `agent run
  failed` or `agent run abandoned`. The subject is built from stored figures and
  never by the model, which is exactly why it is the right place for this: a day
  the agent died still values the portfolio and would otherwise look completely
  ordinary in an inbox.
- **The worst outcome of the day wins**, not the last: a manual retry that
  succeeded does not erase the scheduled run that did not. There is a test.
- `JOB_TIMEOUT_SECONDS` moved from `queries.py` to `repository.py` because both
  the API's run list and the summary now need the same line between a run in
  progress and one killed hard enough never to close its own row (SIGKILL cannot
  be caught). A run past it reads as `abandoned`, never `running`.
- The narrative system prompt tells the model to lead with a failed run and not
  to reason about why it held positions on a day it never ran. `PROMPT_VERSION`
  went to `v3` for that change.

**`Executions` is a gauge sampled per minute, not a counter.** This is the trap
in `locals.alerts.tf` and it was verified against a real run rather than assumed:
the metric reports how many executions are in each state *right now*, so at a
5-minute grain the 06:00 agent job reported `Total` 16 for `Running` and 11 for
`Succeeded` — minutes times executions, which means nothing. The alerts use
`Maximum` over a 15-minute window with a threshold of `0`, which asks "was an
execution in this state at all". A rule written against `Total` would fire
constantly on success.

**Alerts reach subscription Owners by role, not by address.** An
`arm_role_receiver` needs no address in config or state, which is what makes it
usable here — this repository is public and `terraform/environments/*.tfvars`
are committed, the same constraint that put the summary recipient in Key Vault.
Key Vault is *not* an option for this: a `data` source on the vault would fail
any stg or prd plan, since those resource groups do not exist. `TF_VAR_alert_email_address` adds a specific address on top.

**All five metric alerts are one `for_each` resource over `local.metric_alerts`.**
They differ only in what they watch, so the name shape, action group and tags are
written once in `main.alerts.tf` and every rule's settings — and the reason for
each threshold — live beside each other in `locals.alerts.tf`. A sixth rule is an
entry in that map, not another block to keep in step. The `dimension` key is
`null` on the rules that filter nothing and a `dynamic` block turns a non-null one
into a criteria dimension: an empty `dimension` block is not the same as no block
and would match no time series. The budget's two `notification` blocks are left
literal on purpose — two is not repetition, and a `dynamic` block over a
two-element list reads worse than what it replaces.

Other decisions worth not re-litigating:

- **No availability tests.** A synthetic ping keeps a `min_replicas = 0` app
  permanently warm, which costs more than the outage it would detect.
- **No diagnostic setting on the Container Apps environment.** It already ships
  console and system logs to the workspace via `log_analytics_workspace_id`;
  adding one ingests every line a second time against a 0.15 GB/day cap.
- **No `AllMetrics` anywhere.** Metrics are already in the platform metric
  store, free to query and free to alert on. Routing them into Log Analytics
  pays to store a second copy of what the metric alerts read for nothing.
- **Postgres logs are `PostgreSQLLogs` only.** The query-store and session
  categories are per-statement and per-session emissions — the one log shape
  that could actually reach the cap on a database this small.
- **The log quota alert is the expensive one**, because reaching
  `daily_quota_gb` stops ingestion for the rest of the day and nothing in the
  metric store reports it. Evaluated hourly for that reason. Measured ingestion
  is ~0.0005 GB/day against a 0.15 cap, so it is watching for a runaway — a
  crash-looping replica — not for growth. `local.log_daily_quota_gb` feeds both
  the workspace and the threshold so they cannot drift.
- **The budget notifies, it does not stop anything.** Azure budgets never do.
  `budget_start_date` is under `ignore_changes` because Azure refuses a start
  date in a past month on create but reports the stored one thereafter, so
  leaving it tracked would make the first apply of each new month a spurious
  replacement.

## Terraform state and the partial backend

State lives in an Azure storage account. The `backend "azurerm" {}` block in
`versions.tf` is **partial** — per-environment values are in
`terraform/backends/<env>.hcl` (committed; the container is protected by RBAC, not
obscurity), and the storage account is bootstrapped by hand per the README.

The consequence to remember: a partial backend **prompts** on a plain
`terraform init`, and fails outright under `-input=false`. So anything that
doesn't touch state must use `init -backend=false` — the `Makefile`'s `init`,
`validate`, and the CI validate job all do. `plan` uses
`init -reconfigure -backend-config=backends/$(ENV).hcl`. The `terraform_validate`
pre-commit hook already passes `-backend=false` itself. `terraform console`,
unlike `validate`, does need a real backend.

## Environments

`terraform/environments/{dev,stg,prd}.tfvars` hold the `-var-file` inputs;
`make plan ENV=<env>` selects one. All three are planned in CI but **only `dev` is
applied** — a SQL database costs something per environment that exists, and
"production" here still means paper trading, so a second and third copy buys
nothing yet. The plumbing is kept so that changing this is a decision, not a
rebuild. These files are committed intentionally; don't put secrets in them
(`gitleaks` scans as a backstop).

## Scheduling

`agent_cron_expression` (default `30 10 * * *`, every pot at once), `daily_summary_cron_expression`
(default `0 21 * * *`) and `weekly_review_cron_expression` (default
`0 22 * * 0`, Sunday) are **evaluated in UTC**, five fields, no seconds field —
so the wall-clock time shifts with British Summer Time. `schedule_trigger_config`
forces replacement, so a schedule change shows as destroy/create; harmless, since
jobs hold no state.

**The agent runs at 10:30 UTC to dodge DeepSeek's peak window.** Peak is
01:00-04:00 and 06:00-10:00 UTC, Monday to Friday; every other hour is half
price. It was 06:00, squarely in peak; 10:30 rather than 10:00 leaves margin so
a run never straddles the boundary, and still submits hours before the
13:30/14:30 UTC open. `llm/base.py` prices every call at the peak rate
regardless, so **the recorded cost is now double the real bill every day** —
`agent_runs.cost_usd`, the email's spend figures and the weekly review's cost
reasoning all overstate by 2x. DeepSeek's balance, read live, is the true
figure.

**The weekly review must fall after that day's summary.** It reports the
summary's valuation as the week's close, so scheduling it earlier reviews a week
that ends a day short — an ordering the cron expressions do not enforce.
