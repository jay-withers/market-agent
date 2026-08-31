# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Azure infrastructure for **InvestAgent**, an AI paper-trading and investment
research platform: an LLM recommends BUY/SELL/HOLD from news and market data, a
deterministic risk engine decides what's actually permitted, and simulated trades
run against a paper-trading broker. No real money, ever — the experiment is
whether the AI beats a passive index or a savings account over 3–6 months with a
notional £500.

**Only the Terraform infrastructure exists so far.** There is no application code,
no Dockerfile and no image build pipeline. Both container apps and both jobs run
public Microsoft quickstart images as placeholders, which also means the two apps
are publicly reachable unauthenticated pages — authentication belongs with the
real API. Don't invent application code to fill that gap unless asked.

This repo began as a generic Azure Terraform module template and was converted;
if something looks like leftover template scaffolding, it probably is.

## Dev container

The repo is built around the dev container at `.devcontainer/devcontainer.json`,
which uses the image `ghcr.io/jay-withers/dev-container/terraform`. It provides
Terraform, TFLint, terraform-docs, and Checkov, and runs `make install` on
creation. Prefer working inside the container so tool versions match CI.

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

Resource group, user-assigned managed identity, Log Analytics workspace,
Application Insights (workspace-based), Key Vault (RBAC), Container Apps
environment (Consumption-only), two container apps (`api`, `dashboard`), two
scheduled container app jobs (`agent`, `daily-summary`), and an Azure SQL server
plus serverless database. Names come from `Azure/naming/azurerm`.

### The cost constraint drives most of the design

The hard requirement is that **nothing bills meaningfully while idle**. That's
why:

- **There is no Azure Container Registry.** ACR Basic is a flat monthly charge
  with no consumption tier; images will live in ghcr.io. Don't add ACR back
  without raising the cost trade-off.
- **The database is Azure SQL, not PostgreSQL.** PostgreSQL Flexible Server bills
  hourly for as long as it exists and has no auto-pause tier. The consequence for
  future application code is an ODBC driver (`pyodbc`/`aioodbc`), not `psycopg`;
  the relational schema from the project spec is unaffected. If a document still
  says PostgreSQL, it predates this decision.
- **Container apps use `min_replicas = 0`** and the environment has no
  `workload_profile` block. Adding a workload profile introduces a standing
  per-hour charge.
- **`daily_quota_gb = 0.15`** on the Log Analytics workspace, and
  `daily_data_cap_in_gb = 0.1` on Application Insights. Log ingestion is
  otherwise the largest cost risk: Azure Monitor's free grant is 5 GB/month, and
  the default App Insights cap is 100 GB/day.
- **`max_size_gb` is pinned small** on the database — General Purpose storage
  bills the provisioned maximum, not bytes used, and the default is 32 GB.

Traps worth knowing before touching the database: `long_term_retention_policy` and
`azurerm_mssql_server_dns_alias` both **silently prevent auto-pause**, turning it
into a 24/7 bill with no obvious change in the plan. `min_capacity` and
`auto_pause_delay_in_minutes` are only valid on a `GP_S_`/`HS_S_` SKU, and
`license_type` is rejected outright on serverless.

Separately, the largest cost risk in this repo isn't Terraform at all: a
long-lived connection pool in the future API keeps sessions open, so the database
never pauses and bills its 0.5 vCore floor around the clock — order of £150/month
against £4–5 for two scheduled runs a day. Use `NullPool` in the jobs and
`pool_pre_ping` with a short `pool_recycle` in the API.

There is an Azure SQL free offer (100,000 vCore-seconds + 32 GB monthly, forever)
that would make this genuinely free, but `azurerm` exposes no `useFreeLimit`
property in any version — it needs `azapi_resource`. Considered and declined in
favour of a single provider; revisit if the bill becomes annoying.

### Naming

Two requirements from the user drive this: names use the naming module's
**`.name`, not `.name_unique`** (deterministic, no random suffix), and
**every resource name includes `project_name`**. Verified names at
`project_name = "investagent"`, `environment = "dev"`:

```
rg-investagent-dev          cae-investagent-dev     ca-investagent-dev-api        (22/32)
kv-investagent-dev  (18/24) log-investagent-dev     ca-investagent-dev-dashboard  (28/32)
sql-investagent-dev         appi-investagent-dev    caj-investagent-dev-agent     (25/32)
sqldb-investagent-dev       uai-investagent-dev     caj-investagent-dev-summary   (27/32)
```

The daily summary job is named `summary`, not `daily-summary`: the latter would be
33 characters against the 32 container app jobs allow, and the naming module
truncates silently. Its container is still `daily-summary`.

Two consequences of dropping the suffix:

- **Key Vault and SQL server names are globally unique across Azure**, so an apply
  can fail on a name someone else already holds. The fix is to change
  `project_name`, not to reintroduce `.name_unique`.
- A destroyed-and-recreated Key Vault reuses its name, which the soft-delete
  window can hold. `purge_soft_delete_on_destroy = true` in the provider
  `features` block is what keeps that from blocking recreation.

`project_name` is length-validated to 15 characters. The binding constraint is
`ca-<project>-<env>-dashboard` against the 32 characters container apps allow —
not, as you might expect, Key Vault's 24. The four container workloads have their
own naming module instances only so each carries its workload name
(`suffix = [var.project_name, var.environment, "<workload>"]`). Check with
`terraform console` before changing any of it — note `console` needs a real
backend init, unlike `validate`.

Also: `module.naming.sql_database` doesn't exist; the token is `mssql_database`.
The module doesn't lowercase its dashed names, and `mssql_server`,
`container_app` and `container_app_job` all reject uppercase; job names also
reject underscores.

### Security posture

Passwordless throughout. The SQL server sets `azuread_authentication_only = true`,
which satisfies the provider's requirement for an administrator and lets
`administrator_login`/`administrator_login_password` stay unset — no SQL password
exists in source, state or Key Vault. The administrator defaults to whoever runs
`terraform apply` (`locals.database.tf`); applied from CI that's the OIDC service
principal, so set `sql_admin_object_id` — an Entra group is tidiest — before
letting CI apply.

Terraform owns the Key Vault and its RBAC but creates **no**
`azurerm_key_vault_secret`: values are set out of band with
`az keyvault secret set` by whoever holds `Key Vault Secrets Officer` (granted to
the deploying principal automatically, plus `key_vault_administrator_object_ids`).
The workload identity gets read-only `Key Vault Secrets User`. Never add secret
values to Terraform or tfvars.

Granting the managed identity database access needs `CREATE USER ... FROM EXTERNAL
PROVIDER`, T-SQL that the `azurerm` provider cannot execute — it's a documented
one-off manual step in the README, deliberately deferred since no application
needs it yet. The alternative, if it ever becomes annoying, is an Entra security
group as server administrator with the identity as a member (adds the `azuread`
provider and needs directory permissions).

Deliberately destroyable: `purge_protection_enabled = false` on the Key Vault and
`prevent_deletion_if_contains_resources = false` on the provider, both so
`terraform destroy` actually works. That's also why `.tflint.hcl` excludes
`azurerm_key_vault`, `azurerm_mssql_server` and `azurerm_mssql_database` from
`azurerm_resources_missing_prevent_destroy` (verified: those exact three fire) —
excluded rather than disabling the rule, so it stays armed for anything added
later.

### Checkov skips

18 checks fire and every one is suppressed inline with a reason; there are no
global skips and no unnecessary ones (verified by stripping the comments and
re-running). They fall into three groups: features that need standing cost
(auditing, Defender for SQL, private endpoints, zone redundancy), the
destroyability choices above, and `CKV_TF_1` on each `Azure/naming/azurerm`
instance (a Registry module pinned by semver has no commit hash to pin). If you
add a resource, run `checkov` and add skips for exactly what fires — no more.

## Commands

`make` with no target prints the self-documenting help (the default goal).

```bash
make install           # install pre-commit hooks (run once after cloning)
make lint              # run all pre-commit hooks against every file
make fmt               # terraform fmt -recursive
make validate          # terraform init + validate (no Azure credentials)
make plan              # terraform init + plan (set ENV=dev|stg|prd, default dev)
make apply             # terraform init + apply (set ENV=dev|stg|prd, default dev)
```

There is no `make test`: the tests were removed at the user's request.
`make validate` is the credential-free check.

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

`agent_cron_expression` (default `0 6 * * *`) and `daily_summary_cron_expression`
(default `0 21 * * *`) are **evaluated in UTC**, five fields, no seconds field —
so the wall-clock time shifts with British Summer Time. `schedule_trigger_config`
forces replacement, so a schedule change shows as destroy/create; harmless, since
jobs hold no state.

## Commit messages

Commits must follow [Conventional Commits](https://www.conventionalcommits.org/) — enforced by commitlint at commit-msg time. Examples: `feat: add container apps jobs`, `fix: correct key vault name length`, `chore: bump azurerm provider`.

## Pre-commit config

Hooks are in `.pre-commit-config.yaml` at the repo root. The `no-commit-to-branch`
hook blocks direct commits to `main`. `terraform_fmt`/`terraform_validate`/
`terraform_docs` come from `antonbabenko/pre-commit-terraform`. TFLint and Checkov
are run by local hooks instead of that repo's `terraform_tflint`/
`terraform_checkov`: `scripts/tflint-per-env.sh` and `scripts/checkov-per-env.sh`
each glob `terraform/environments/*.tfvars` and run their tool once per
environment with `--var-file`, so rules that depend on concrete variable values
(naming, tags, region-specific checks) are evaluated against what each environment
actually deploys — dropping a new environment's tfvars in `terraform/environments/`
picks it up automatically. `tflint-per-env.sh` also discovers every directory under
`terraform/` containing `.tf` files, since tflint (unlike checkov) doesn't recurse;
today that's just the root configuration. Both scripts prune hidden directories via
`-name '.?*'`, not `-name '.*'` — the latter also matches the find root `.` itself
and would silently prune everything. `checkov-per-env.sh` deliberately does **not**
pass `--download-external-modules`: it cost ~15s per invocation regardless of
caching (checkov's own graph-building overhead, not network time) for zero benefit,
since `Azure/naming/azurerm` has no resources of its own. Checkov logs a harmless
"Failed to download module" warning as a result.

`terraform/README.md` is generated by terraform-docs between the `BEGIN_TF_DOCS`/
`END_TF_DOCS` markers — never hand-edit inside those. The first `make lint` after
changing variables or outputs will report `terraform_docs` as failed because it
rewrote the file; re-run and it passes.

## CI

Workflows are prefixed `ci-` (pull-request checks) or `cd-` (post-merge delivery):

- **ci-pre-commit**: runs all linters on PRs to `main` via the `pre-commit` job,
  which calls the reusable workflow
  `jay-withers/template-pipelines/.github/workflows/pre-commit.yml` (pinned by
  commit SHA, with the tag as a comment). Because it's a reusable-workflow call,
  the status check context it reports is `pre-commit / Pre-commit`
  (`<caller job id> / <reusable job name>`), not the bare `pre-commit` job id.
- **ci-terraform**: a `changes` job (dorny/paths-filter) gates a `validate` job
  (`init -backend=false` + `validate`, no credentials) and a `plan` job (matrixed
  over `environment: [dev, stg, prd]` via Azure OIDC). The plan job is
  additionally gated on `if: vars.AZURE_CLIENT_ID != ''`, so it stays skipped
  until the `AZURE_CLIENT_ID`/`AZURE_TENANT_ID`/`AZURE_SUBSCRIPTION_ID`
  repository variables are set — all three matrix legs currently share one
  subscription. The `ci-terraform` gate job always runs and is the check to
  require in branch protection; path filtering is at the job level (not the
  workflow trigger) precisely so the required check always reports.
- **cd-tag**: auto-creates a semver tag on every merge to `main` (default bump:
  patch).

## Renovate

`renovate.json` extends the shared presets from
[`jay-withers/template-renovate`](https://github.com/jay-withers/template-renovate)
(`github>jay-withers/template-renovate`) rather than configuring Renovate inline —
that one line pulls in `config:recommended`, semantic commits, automerge/schedule
policy, and per-ecosystem grouping. Only what's genuinely specific to this repo
stays local: `autoApprove` (so Renovate's own PRs clear the branch-protection
review requirement) and two `regexManagers` (`.terraform-version`, and
`.tflint.hcl`'s azurerm plugin version pin) — neither is a GitHub Action, npm/pip
package or Docker image that a shared preset's manager already covers. Change
ecosystem-wide policy in `template-renovate` itself so every consuming repo picks
it up; only touch this file for something unique here.

## GitHub repo settings

Branch protection and other repo-level GitHub settings aren't templated as files
here. This repo is managed centrally by
[jay-withers/github-repos](https://github.com/jay-withers/github-repos)'s
Terraform root module, which is the single source of truth for every jay-withers
repo. The required status check should be `ci-terraform`.
