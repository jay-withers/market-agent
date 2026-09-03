# InvestAgent — infrastructure

Azure infrastructure for **InvestAgent**, an AI paper-trading and investment
research platform: an LLM analyses financial news and market data and recommends
BUY/SELL/HOLD, a deterministic risk engine decides what is actually permitted,
and the resulting simulated trades run against a paper-trading broker. No real
money is ever connected. The point of the experiment is to find out, over three
to six months, whether the AI beats simply putting £500 into a passive index or
leaving it in a savings account.

This repository contains the Terraform infrastructure and the **application**,
which runs locally but is **not yet deployed** — see
[The application](#the-application) and [Deploying](#deploying).

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
| PostgreSQL Flexible Server + database | Burstable B1ms, Entra-only authentication. The one resource that bills while idle |

Two departures from the original design:

- **No Azure Container Registry**, driven by the requirement that nothing bills
  meaningfully while idle. ACR Basic is a flat monthly charge with no consumption
  tier. Images will live in ghcr.io instead. The placeholder images are public, so
  nothing needs a registry yet.
- **PostgreSQL, not Azure SQL** — and this one was forced rather than chosen.
  Azure SQL serverless auto-pauses to near zero and would have been the cheaper
  design, but this subscription cannot provision Azure SQL in any region policy
  allows. See [Cost model](#cost-model) below. The consequence for application code
  is `psycopg` rather than an ODBC driver (`pyodbc`/`aioodbc`); the relational
  schema is unaffected.

## The application

`apps/investagent/` is one Python package with three entrypoints (`api`, `agent`,
`summary`) sharing one image: they have the risk engine, the database layer, the
broker client and the LLM client in common, so three images would mean three
builds of near-identical layers. The dashboard is genuinely separate and gets
its own.

| Module | What it does |
| --- | --- |
| `settings.py` | Configuration, and secrets from an env var first then Key Vault |
| `db.py` | `psycopg` pool authenticated with an Entra token instead of a password |
| `models.py` | Domain models, risk configuration, and the LLM's output types |
| `risk.py` | The deterministic risk engine — a pure function |

```bash
make install   # pre-commit hooks and Python dependencies (needs re-running after a rebuild)
make test      # pytest
```

Money is `Decimal` throughout, matching the `NUMERIC(18,4)` columns in `sql/`,
and quantized **towards zero** so a rounding step can never approve a trade over
a limit.

**The risk engine is the part that matters.** The LLM recommends; the engine
decides. It runs gates first — a HOLD, a ticker off the allowlist, confidence
under the floor, the daily trade budget spent, a sell with nothing held — and
then applies a cap per limit, of which the smallest wins. It can only refuse or
approve less, never invent a trade, and nothing the model writes in its
reasoning can raise a limit. Every verdict records each cap it considered and
the single constraint that decided the outcome, which is what gets written to
`ai_decisions.risk_verdict`.

## Running it locally

```bash
make up          # Postgres with the schema baked in, the API, and the dashboard
make run-agent   # one agent run: real market data and LLM calls, no orders
make down        # stop, and delete the data volume
```

The dashboard is then on <http://localhost:8080> and the API on
<http://localhost:8000>. Note that published ports land on the Docker *host*, so
from inside the dev container reach a service at its bridge IP or via
`host.docker.internal`.

Secrets come from Key Vault, not a file: `make run-agent` mints a short-lived,
Key Vault-scoped token from your `az login` and passes it in, because the image
has no `az` and so cannot authenticate on its own. A `.env` works too — see
`.env.example` — for anyone with no Azure access.

`DRY_RUN` defaults to **true**, so the whole decision path runs and is recorded
but no order is ever submitted.

## Deploying

Terraform points all four workloads at real ghcr.io images and `terraform plan`
is clean, but **nothing is deployed yet** — the images have not been pushed. In
order:

```bash
gh auth refresh --scopes write:packages,read:packages   # the token needs write:packages
make build push                                        # linux/amd64, tagged with the git SHA
# then flip both ghcr packages to public, once
make deploy
```

Both apps currently serve public Microsoft quickstart placeholder pages, so
applying before the images exist would replace four working pages with four
revisions that cannot pull.

`--platform linux/amd64` is not optional: Container Apps runs amd64 only, and a
native build on an Apple Silicon host produces an image that crash-loops with an
exec format error and no other clue. The Makefile does it rather than leaving it
to memory.

**Both apps are publicly reachable and unauthenticated**, deliberately: the API
is read-only, so it cannot place a trade or alter a decision however it is
called, and no response carries a secret, any PII, or real money. A shared
bearer sits behind `API_REQUIRE_TOKEN` for when that changes; the proper fix is
Container Apps EasyAuth with Entra, which `azurerm` does not expose.

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

Everything except the database costs nothing while idle. The database does not,
and that is not a configuration mistake — see below.

| | Idle cost |
| --- | --- |
| Container apps | £0 — `min_replicas = 0`, Consumption plan |
| Container app jobs | £0 — billed only while a scheduled run is in flight |
| Key Vault | Effectively £0 — priced per operation |
| Log Analytics / App Insights | £0 up to the 5 GB/month free grant, which `daily_quota_gb = 0.15` keeps ingestion inside |
| Managed identity, resource group | Free |
| PostgreSQL Flexible Server | **~£13/month, always** — ~£9.71 B1ms compute plus ~£3 storage |

### Why the database bills while idle

PostgreSQL Flexible Server has **no serverless or auto-pause tier**. It bills its
SKU per hour for as long as it exists. That is a property of the service, not
something the configuration can tune away — the only lever is stopping the server,
and a stopped server auto-restarts after seven days.

Azure SQL serverless *does* auto-pause, and would have cost roughly £0. It was the
original design. It is not used because **this subscription cannot provision Azure
SQL in any region it is allowed to deploy to**:

- The Azure Policy assignment `allowed-locations-dev` has effect **Deny** and
  permits only `westeurope` and `northeurope`.
- The subscription is separately restricted from provisioning Azure SQL in both.

That restriction is regional, not per-SKU: in both regions all 208 Azure SQL
service objectives across all ten editions report `Visible` rather than
`Available`, `Free` and `Basic` included. No smaller SKU gets around it.

PostgreSQL is restricted in `westeurope` but open in `northeurope`, which is why
`var.location` defaults there and why PostgreSQL is the engine.

**To get back to a near-free database**, amend `allowed-locations-dev` to admit a
SQL-capable region (`francecentral`, `swedencentral`, `germanywestcentral`,
`switzerlandnorth`, `italynorth` and `spaincentral` are all verified available),
then switch back to Azure SQL — ideally on its free offer, which grants 100,000
vCore-seconds and 32 GB monthly, permanently. That needs `azapi_resource`, since
`azurerm` exposes no `useFreeLimit`.

One thing that did get simpler: the Azure SQL design carried a **£150/month**
trap, where a long-lived connection pool prevented auto-pause and left the
database billing around the clock. With an always-on server that risk does not
exist. Connection pooling is now free to use.

### Checking region availability

The provider's advertised location list is not subscription-aware. The
capabilities APIs are — query those rather than guessing:

```bash
SUB=$(az account show --query id -o tsv)

# PostgreSQL: want restricted "Disabled"
az rest --method get \
  --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.DBforPostgreSQL/locations/<region>/capabilities?api-version=2023-06-01-preview" \
  --query "value[0].{restricted:restricted,reason:reason}"

# Azure SQL: want status "Available", not "Visible"
az rest --method get \
  --url "https://management.azure.com/subscriptions/$SUB/providers/Microsoft.Sql/locations/<region>/capabilities?api-version=2023-08-01" \
  --query "{status:status,reason:reason}"
```

Note also that `Microsoft.App` must be registered on the subscription
(`az provider register --namespace Microsoft.App`), or the Container Apps
environment fails with `MissingSubscriptionRegistration`.

## Environments

`terraform/environments/{dev,stg,prd}.tfvars` hold per-environment inputs.
`make plan` picks one via `ENV` (default `dev`):

```bash
make plan            # plans with environments/dev.tfvars
make plan ENV=stg
make plan ENV=prd
```

All three are planned in CI, but **only `dev` is actually applied.** Unlike
scale-to-zero compute, each PostgreSQL server that exists costs ~£13/month whether
used or not, and "production" here still means paper trading — so a second and
third copy would triple the only bill this project has, for nothing. The plumbing is kept so that changing this later is a decision, not a
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

The PostgreSQL server has **no password**: `password_auth_enabled` is off and
Entra is the only way in. The administrator is a named user hardcoded in
`terraform/main.database.tf` — object ID, principal name and type — so it stays
the same principal no matter who or what applies, including from CI.

Changing the administrator is therefore a code edit rather than a tfvars change.
An Entra group is worth considering if more than one person needs access: it
takes `principal_type = "Group"` and a group created out of band.

Granting the managed identity access needs SQL that Terraform cannot execute, and
so does the schema. That SQL lives in `sql/`, one self-contained file per task,
run by `scripts/Invoke-DbSql.ps1`:

```powershell
./scripts/Invoke-DbSql.ps1                             # all of sql/, in filename order
./scripts/Invoke-DbSql.ps1 -Path ./sql/001-schema.sql  # one file
./scripts/Invoke-DbSql.ps1 -Path ./sql/001-schema.sql,./sql/002-seed-watchlist.sql
```

The script adds a temporary firewall rule for your public IP (the "allow Azure
services" rule doesn't cover your machine), connects as the signed-in user with
an Entra access token in place of the password, runs each file in turn, then
offers to remove the rule again — answer `n` to keep it while iterating, since
firewall changes are slow and serialise on the server. It needs `psql` and `az`
on PATH, and takes the dev resource names as parameter defaults.

It carries a `pwsh` shebang and is executable, so those commands work unchanged
from bash — a comma-separated `-Path` is the one thing that needs a `pwsh` prompt,
since `pwsh -File` passes arguments as plain strings and won't parse it as an
array.

Everything in one invocation shares that one firewall rule and one access token,
which is the whole reason it takes more than one file: a rule per migration would
spend most of the run waiting on the server.

Migrations are numbered (`001-schema.sql`), written so re-running them is a no-op,
and record themselves in a `schema_migrations` table. There are no
down-migrations — reversing something is a new numbered file.

**`sql/grant-uai-access.sql` has already been run** — the identity has `CONNECT`,
`USAGE, CREATE ON SCHEMA public`, privileges on the tables that exist, and default
privileges covering any created later. It's safe to re-run, in any order relative
to the migrations.

Two traps worth knowing if you write another file:

- **`pgaadauth` is installed only in the `postgres` maintenance database**, not in
  the application database, so `pgaadauth_create_principal` has to run there or it
  fails with `function ... does not exist`. Roles are cluster-wide, so the file
  does the create after `\connect postgres` and then switches back for the GRANTs.
- **The Entra administrator's registered `principal_name` is the role name**, so it
  has to be the UPN. A display name there means `psql user=<UPN>` fails with
  `password authentication failed`, which looks like a token problem and isn't.

Application code connects with `psycopg`, using the managed identity's access
token as the password and the `AZURE_CLIENT_ID` already passed into every
container.

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
  locals.database.tf            # PostgreSQL administrator defaults
  main.tf                       # naming module + resource group
  main.identity.tf              # user-assigned managed identity
  main.observability.tf         # log analytics + application insights
  main.key-vault.tf             # key vault + role assignments
  main.container-apps-env.tf    # container apps environment
  main.container-apps.tf        # api + dashboard container apps
  main.container-apps-jobs.tf   # agent + daily-summary scheduled jobs
  main.database.tf              # postgresql flexible server, database, firewall rule
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
