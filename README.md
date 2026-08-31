# InvestAgent — infrastructure

Azure infrastructure for **InvestAgent**, an AI paper-trading and investment
research platform: an LLM analyses financial news and market data and recommends
BUY/SELL/HOLD, a deterministic risk engine decides what is actually permitted,
and the resulting simulated trades run against a paper-trading broker. No real
money is ever connected. The point of the experiment is to find out, over three
to six months, whether the AI beats simply putting £500 into a passive index or
leaving it in a savings account.

This repository currently contains **only the Terraform infrastructure**. There
is no application code yet — see [Out of scope](#out-of-scope-right-now).

## What gets deployed

| Resource | Purpose |
| --- | --- |
| Resource group | Everything below lives here |
| User-assigned managed identity | Shared by all four workloads; reads Key Vault, and authenticates to SQL once there's an app |
| Log Analytics workspace | Container and job logs, with a daily ingestion cap |
| Application Insights | Workspace-based, for future application telemetry |
| Key Vault | RBAC-authorised. Terraform owns the vault, **not** the secret values |
| Container Apps environment | Consumption-only, so idle costs nothing |
| Container app `api` | FastAPI backend (placeholder image), scales to zero |
| Container app `dashboard` | React dashboard (placeholder image), scales to zero |
| Container app job `agent` | Scheduled: news → analysis → risk engine → simulated trade |
| Container app job `daily-summary` | Scheduled: performance, benchmarks, email |
| Azure SQL server + database | Serverless, auto-pausing, Entra-only authentication |

Two deliberate departures from the original design, both driven by the
requirement that nothing bills meaningfully while idle:

- **No Azure Container Registry.** ACR Basic is a flat monthly charge with no
  consumption tier. Images will live in ghcr.io instead. The placeholder images
  are public, so nothing needs a registry yet.
- **Azure SQL, not PostgreSQL.** PostgreSQL Flexible Server bills per hour for as
  long as it exists, with no serverless or auto-pause tier. Azure SQL serverless
  pauses when idle and bills compute per vCore-second. The consequence for
  application code is an ODBC driver (`pyodbc`/`aioodbc`) rather than `psycopg`;
  the relational schema is unaffected.

## Out of scope right now

No application code, no Dockerfiles, no image build pipeline. Both container apps
and both jobs run public Microsoft quickstart images, which means the two apps are
currently **publicly reachable, unauthenticated placeholder pages**. Authentication
belongs with the real API.

Replacing a placeholder with a real image means adding a `registry` block and a
Key Vault-backed `secret` for the pull token — see `locals.container-apps.tf`.

## Getting started

Open the repository in the dev container (VS Code: **Reopen in Container**, or
GitHub Codespaces). It ships with Terraform, TFLint, terraform-docs and Checkov,
and runs `make install` on creation to wire up the pre-commit hooks.

Outside a dev container, install the hooks manually:

```bash
make install
```

## Commands

Run `make` (or `make help`) to list the available targets:

```bash
make install           # install pre-commit hooks (run once after cloning)
make lint              # run all pre-commit hooks against every file
make fmt               # terraform fmt -recursive
make validate          # terraform init + validate (no Azure credentials needed)
make plan              # terraform init + plan (set ENV=dev|stg|prd, default dev)
make apply             # terraform init + apply (set ENV=dev|stg|prd, default dev)
```

`validate` initialises with `-backend=false`, so it works with no Azure
credentials and no state. `plan` and `apply` need both — see below. `apply` does
not pass `-auto-approve`: it creates resources that bill.

## Terraform state

State lives in an Azure storage account, configured **partially**: the backend
block in `versions.tf` is empty and the per-environment values live in
`terraform/backends/<env>.hcl`. Those files are committed; the container is
protected by Azure RBAC, not by keeping its name private.

Anything that doesn't touch state must initialise with `-backend=false`, or the
partial configuration prompts for the missing values (and fails outright under
`-input=false`, as in CI).

The storage account (`sttfsharedjw`) is shared across repositories, with **one
container per repository** — this one uses `market-agent`. Creating the container
is the only bootstrap step for a new repo:

```bash
az storage container create \
  --name market-agent \
  --account-name sttfsharedjw \
  --auth-mode login
```

Then `make plan` (which runs `terraform init -backend-config=backends/dev.hcl`
for you).

## Cost model

The whole configuration is built so that an idle month costs almost nothing:

| | Idle cost |
| --- | --- |
| Container apps | £0 — `min_replicas = 0`, Consumption plan |
| Container app jobs | £0 — billed only while a scheduled run is in flight |
| Key Vault | Effectively £0 — priced per operation |
| Log Analytics / App Insights | £0 up to the 5 GB/month free grant, which `daily_quota_gb = 0.15` keeps ingestion inside |
| Managed identity, resource group | Free |
| Azure SQL | Storage only while paused (a couple of GB); compute per vCore-second while awake |

Azure SQL is therefore the only meaningful line. Two things to know about it:

- **Each wake bills a minimum idle window.** The database pauses only after
  `auto_pause_delay_in_minutes` (15, Azure's minimum) with no sessions, so a job
  run costs its own duration plus 15 minutes. At roughly 20 minutes online per
  run, two runs a day is about 36,000 vCore-seconds a month — on the order of
  £4–5.
- **A connection pool defeats auto-pause entirely, and that is expensive.** Any
  live replica holding an idle connection keeps sessions open, so the database
  never pauses and bills its 0.5 vCore floor around the clock: roughly 1.3 million
  vCore-seconds a month, on the order of **£150**. That is not a rounding error on
  the £4–5 above — it is the single largest financial risk in this repository, and
  it will be caused by application code, not by Terraform. When the real API
  arrives, give it `pool_pre_ping` and a short `pool_recycle`, and use `NullPool`
  in the jobs. Consider a budget alert on the subscription as a backstop.

(Both figures are list-price estimates at £0.40–0.45 per vCore-hour; check the
Azure pricing calculator for your region before relying on them.)

There is also an Azure SQL free offer (100,000 vCore-seconds and 32 GB a month,
permanently) that would make this genuinely free, but `azurerm` doesn't expose
the `useFreeLimit` property in any version — it would need an `azapi_resource`.
Worth revisiting if the bill becomes irritating.

## Environments

`terraform/environments/{dev,stg,prd}.tfvars` hold per-environment inputs.
`make plan` picks one via `ENV` (default `dev`):

```bash
make plan            # plans with environments/dev.tfvars
make plan ENV=stg
make plan ENV=prd
```

All three are planned in CI, but **only `dev` is actually applied.** Unlike
scale-to-zero compute, a SQL database costs something per environment that exists,
and "production" here still means paper trading — so a second and third copy buys
nothing yet. The plumbing is kept so that changing this later is a decision, not a
rebuild.

These files are **committed, not gitignored** — see `.gitignore`. Don't put
secrets in them; `gitleaks` (pre-commit and CI) scans every commit as a backstop.

## Secrets

Terraform creates the Key Vault and grants access to it. It deliberately creates
**no secret values** — there is no `azurerm_key_vault_secret` anywhere, so nothing
sensitive reaches Terraform source or state.

Whoever runs `terraform apply` is granted `Key Vault Secrets Officer`
automatically (add others via `key_vault_administrator_object_ids`), and populates
the secrets by hand:

```bash
az keyvault secret set --vault-name "$(terraform -chdir=terraform output -raw key_vault_name)" \
  --name OPENAI-API-KEY --value '...'
```

The workload identity holds only `Key Vault Secrets User` — read, not write.

## Database access

The SQL server has **no password**: `password_auth_enabled` is off and Entra is
the only way in. The administrator defaults to whoever runs `terraform apply`.

Applied from CI, that means the OIDC service principal becomes the administrator
and no human can connect. Set `sql_admin_object_id` — ideally to an Entra group
containing both you and anything else that needs access — before letting CI apply.

Granting the managed identity access needs T-SQL, which Terraform cannot execute,
so it is a one-off manual step per database. Nothing needs it until there's an
application:

```bash
cd terraform
FQDN=$(terraform output -raw sql_server_fqdn)
DB=$(terraform output -raw sql_database_name)
IDENTITY=$(terraform output -raw identity_name)
SERVER=${FQDN%%.*}
MY_IP=$(curl -s ifconfig.me)

# The "allow Azure services" rule doesn't cover your machine.
az sql server firewall-rule create -g "$(terraform output -raw resource_group_name)" \
  -s "$SERVER" -n devbox --start-ip-address "$MY_IP" --end-ip-address "$MY_IP"

# The first attempt may fail with error 40613 while the database resumes; retry.
sqlcmd -S "$FQDN" -d "$DB" -G -Q "
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'$IDENTITY')
  CREATE USER [$IDENTITY] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [$IDENTITY];
ALTER ROLE db_datawriter ADD MEMBER [$IDENTITY];
ALTER ROLE db_ddladmin  ADD MEMBER [$IDENTITY];"

az sql server firewall-rule delete -g "$(terraform output -raw resource_group_name)" \
  -s "$SERVER" -n devbox
```

Application code then connects with `Authentication=ActiveDirectoryMsi` and the
`AZURE_CLIENT_ID` already passed into every container.

## Scheduling

Job schedules are `agent_cron_expression` (default `0 6 * * *`) and
`daily_summary_cron_expression` (default `0 21 * * *`). Both are **evaluated in
UTC** — five fields, no seconds — so the wall-clock time shifts by an hour with
British Summer Time.

Changing a schedule replaces the job rather than updating it
(`schedule_trigger_config` forces replacement), which shows up as a
destroy/create in the plan. Harmless: jobs hold no state.

## File layout convention

Every configuration follows a fixed layout, enforced rather than merely
documented: `locals`/`variable`/`output`/`data` blocks must live in a matching
`locals.tf`/`variables.tf`/`outputs.tf`/`data.tf` — or a topic-scoped variant such
as `main.database.tf` or `variables.optional.tf` — and `terraform{}`/`provider{}`
blocks in `versions.tf`, via the local pre-commit hook `check-tf-standards`
(`scripts/check-tf-standards.sh`). `main.tf` is left for resources and modules.

Variables are split by whether they must be supplied:
`variables.required.tf` (just `environment`) and `variables.optional.tf`
(everything with a default).

## Linting against every environment

`make lint` runs TFLint and Checkov once per file in `terraform/environments/`
(via `scripts/tflint-per-env.sh` and `scripts/checkov-per-env.sh`, wired in as
local pre-commit hooks) rather than once with unresolved variables. Passing each
environment's real `-var-file` gives rules that depend on concrete values —
naming, tags, region-specific checks — something to actually evaluate. Both
scripts glob the tfvars at run time, so adding an environment is enough to get it
linted.

Checkov failures are suppressed with inline `checkov:skip` comments and a reason,
never globally. Every skip currently in the configuration corresponds to a check
that genuinely fires and a deliberate cost or destroyability trade-off.

## Azure auth for `terraform plan`

`make plan` uses whatever the `azurerm` provider picks up normally — `az login`,
or `ARM_*` environment variables.

In CI, the `ci-terraform` **plan** job runs once per environment using GitHub
OIDC (no long-lived secrets). It is **skipped until you set the
`AZURE_CLIENT_ID` repository variable**, so the check stays green before Azure
auth is wired up. The **validate** job needs no credentials and always runs on a
Terraform change. All three environments currently share one subscription; the
plan job's matrix is where to split per-environment credentials if that changes.

To enable it:

1. Create an Azure app registration or managed identity and add a **federated
   credential** trusting this repository's GitHub Actions.
2. Grant it the roles it needs on the target subscription.
3. Add three **repository variables** (Settings → Secrets and variables →
   Actions → Variables): `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`.

## Repository settings

Branch protection and other repo-level GitHub settings aren't templated here.
This repository is managed centrally by
[jay-withers/github-repos](https://github.com/jay-withers/github-repos)'s
Terraform root module. Mark **`ci-terraform`** as the required status check — it
always runs and aggregates the validate and plan jobs.

## Structure

```text
.devcontainer/
  devcontainer.json    # dev container (ghcr.io/jay-withers/dev-container/terraform)
.terraform-version     # pinned Terraform version (tfenv/tenv + CI)
terraform/
  versions.tf                   # terraform{}, required_providers, backend, provider
  data.tf                       # azurerm_client_config
  locals.tf                     # default tags
  locals.container-apps.tf      # placeholder images, container sizing, shared env
  locals.database.tf            # SQL administrator defaults
  main.tf                       # naming module + resource group
  main.identity.tf              # user-assigned managed identity
  main.observability.tf         # log analytics + application insights
  main.key-vault.tf             # key vault + role assignments
  main.container-apps-env.tf    # container apps environment
  main.container-apps.tf        # api + dashboard container apps
  main.container-apps-jobs.tf   # agent + daily-summary scheduled jobs
  main.database.tf              # azure sql server, database, firewall rule
  variables.required.tf         # inputs with no default
  variables.optional.tf         # inputs with defaults
  outputs.tf
  README.md                     # generated by terraform-docs
  .terraform-docs.yml
  .tflint.hcl                   # TFLint config (terraform + azurerm rulesets)
  backends/
    dev.hcl                     # partial backend config per environment
    stg.hcl
    prd.hcl
  environments/
    dev.tfvars                  # -var-file inputs per environment (committed)
    stg.tfvars
    prd.tfvars
.pre-commit-config.yaml
commitlint.config.js
CONTRIBUTING.md
.github/
  CODEOWNERS
  pull_request_template.md
  ISSUE_TEMPLATE/
  workflows/
    ci-pre-commit.yml    # lints all files on PRs to main
    ci-terraform.yml     # terraform validate + plan (matrix over dev/stg/prd)
    cd-tag.yml           # auto-tags on merge to main (semver patch bump)
renovate.json
scripts/
  check-tf-standards.sh
  tflint-per-env.sh
  checkov-per-env.sh
Makefile
```

## Roadmap

Infrastructure is the first slice. Still to come: the Python agent (market data,
news, LLM analysis, risk engine), the paper-trading integration, the database
schema, the FastAPI API, the React dashboard, container images and a build
pipeline, monitoring alerts, and finally the three-to-six-month evaluation against
S&P 500, FTSE 100, a global index and cash.
