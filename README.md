# MarketAgent — infrastructure

Azure infrastructure for **MarketAgent**, an AI paper-trading and investment
research platform: an LLM analyses financial news and market data and recommends
BUY/SELL/HOLD, a deterministic risk engine decides what is actually permitted,
and approved orders run against Alpaca's paper-trading broker. No real money is
ever connected. The experiment mirrors the $100,000 paper account and compares
its performance with passive indexes and a savings benchmark.

This repository contains the Terraform infrastructure and the **application**,
both deployed and running — see [The application](#the-application) and
[Deploying](#deploying).

## What gets deployed

| Resource | Purpose |
| --- | --- |
| Resource group | Application resources; dev uses shared platform resources in `rg-platform-dev` |
| User-assigned managed identity | Shared by all five workloads; reads Key Vault and the database blob |
| Log Analytics workspace | Dev uses existing `log-platform-dev` |
| Application Insights | Dev uses existing `appi-platform-dev` |
| Key Vault | RBAC-authorised. Terraform owns the vault, **not** the secret values |
| Container Apps environment | Dev uses existing `cae-platform-dev`, on its Consumption profile |
| Container app `api` | Read-only FastAPI backend, scales to zero |
| Container app `dashboard` | React dashboard on nginx, scales to zero |
| Container app job `agent` | Scheduled: news → analysis → risk engine → Alpaca paper order |
| Container app job `sync` | Every five minutes: mirror Alpaca cash, equity, positions and open orders |
| Container app job `daily-summary` | Scheduled: performance, benchmarks, email |
| Container app job `weekly-review` | Scheduled Sunday: reviews the week, proposes changes |
| Storage account `stmarketagentdev` | The database: one DuckDB file in the `database` container. Versioned, soft-deleted, delete-locked |
| Action group | Where alerts go — subscription Owners, by role rather than by address |
| Log alerts | Dedicated workspaces get an ingestion-quota alert; the shared platform monitors job failures |
| Budget | Monthly Azure spend, notifying at 80% actual and 100% forecast |
| Diagnostic setting | Key Vault audit events |

Two departures from the original design:

- **No Azure Container Registry**, driven by the requirement that nothing bills
  meaningfully while idle. ACR Basic is a flat monthly charge with no consumption
  tier. The images live in public ghcr.io packages instead, which also means no
  workload needs a `registry` block or a pull secret.
- **No database server.** The database is a DuckDB file in blob storage, which
  the jobs lease while they write and the API reads a copy of. It replaced a
  PostgreSQL Flexible Server (Azure SQL couldn't be provisioned here at all),
  which billed every hour for a database touched for minutes a day. See
  [Cost model](#cost-model) below.

## The application

`apps/marketagent/` is one Python package with five entrypoints (`api`, `agent`,
`sync`, `summary`, `weekly`) sharing one image: they have the risk engine, the database
layer, the broker client and the LLM client in common, so four images would mean
four builds of near-identical layers. The dashboard is genuinely separate and gets
its own.

| Module | What it does |
| --- | --- |
| `settings.py` | Configuration, and secrets from an env var first then Key Vault |
| `db.py` | The DuckDB file in blob storage: a lease per write, an ETag-checked copy for reads |
| `models.py` | Domain models, risk configuration, and the LLM's output types |
| `risk.py` | The deterministic risk engine — a pure function |

```bash
make install   # pre-commit hooks and Python dependencies (needs re-running after a rebuild)
make test      # pytest
```

Money is `Decimal` throughout, matching the `DECIMAL(18,4)` columns in `schema/`,
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
make up          # Azurite (holding the database blob), the API, and the dashboard
make demo        # same stack, loaded with dummy $100,000 account data
make run-agent   # one agent run: real market data, LLM calls, Alpaca paper orders
make down        # stop, and delete the data volume
```

The dashboard is then on <http://localhost:8080> and the API on
<http://localhost:8000>. Note that published ports land on the Docker *host*, so
from inside the dev container reach a service at its bridge IP or via
`host.docker.internal`.

The dashboard accounts in USD to match Alpaca. Its `$ USD` / `£ GBP` control
can convert every displayed amount using the latest reference rate; changing
the display currency does not alter stored values or order sizes.

Secrets come from Key Vault, not a file: `make run-agent` mints a short-lived,
Key Vault-scoped token from your `az login` and passes it in, because the image
has no `az` and so cannot authenticate on its own. A `.env` works too — see
`.env.example` — for anyone with no Azure access.

`DRY_RUN` defaults to **false**, so approved trades are submitted to Alpaca's
paper account. Set `DRY_RUN=true` to record simulated fills without submitting
orders, including locally with `DRY_RUN=true make run-agent`.

Azure's agent job sets this explicitly through Terraform's `agent_dry_run`
variable. It defaults to `false` in every environment. The job also pins
`ALPACA_TRADING_BASE_URL` to `https://paper-api.alpaca.markets`.

The job's environment variables are covered by `lifecycle.ignore_changes`, so
Terraform seeds these settings when creating a job but does not update them on
an existing job. When changing mode, update the environment's tfvars and the
running job together. For dev, the following has already been applied:

```bash
az containerapp job update --name caj-marketagent-dev-agent \
  --resource-group rg-marketagent-dev \
  --set-env-vars DRY_RUN=false ALPACA_TRADING_BASE_URL=https://paper-api.alpaca.markets
```

`make deploy` preserves these settings when updating the image.

## Deploying

All five workloads run from public ghcr.io images. **Merging to `main` builds,
pushes and deploys them**: `cd-tag` bumps the semver tag, then `cd-publish`
builds both images on a native amd64 runner, pushes each under two immutable
tags — the release (`v1.2.3`) and the commit's short SHA, which is what
`IMAGE_TAG` already defaults to — and its `deploy` job runs this same
`make deploy IMAGE_TAG=<release>` against dev, through the shared
`container-apps-deploy.yml` (OIDC in the `dev` GitHub environment, as
`uai-market-agent-deploy-dev`). To redeploy an existing tag, run `cd-publish`
from the Actions tab with it.

**Every deploy is smoke-tested, and a failed one rolls back.** Before deploying,
the workflow records `make deployed-tag` (the tag on the API now). After it,
`make smoke` (`scripts/smoke.sh`) checks the API and the dashboard: each is
configured with the new tag, its `/healthz` on the default FQDN reports it as
`version` (allowing five minutes for a cold start), and `/readyz` answers 200.
It then starts one run of every job (each pot's agent and sync, the summary and
the weekly review) with its args swapped for `marketagent selfcheck`, which
checks the settings, reading the database blob and every secret a run reads, and places no
order, calls no LLM and sends nothing; and waits for each to succeed. If the
deploy or the smoke test fails, the workflow redeploys the recorded tag and
fails the run, so a `deploy-failed` issue says whether dev is back on the old
version. Rolling back moves the images only, and a newer schema file stays
applied, so a release must not add a schema the previous version can't read.

```bash
make deploy IMAGE_TAG=v1.2.3   # what cd-publish runs: update every app and job to a published tag
make smoke  IMAGE_TAG=v1.2.3   # and what it checks afterwards
```

Building locally is still there for iteration, and is the only way to test an
image before merge:

```bash
make build push   # linux/amd64, tagged with the git SHA
make deploy       # update existing apps and jobs to that same tag
```

The push needs a `gh` token carrying `write:packages`
(`gh auth refresh --scopes write:packages,read:packages`), and ghcr defaults a
*new* package to private regardless of repository visibility — so both packages
needed flipping to public once, after the first push, or Container Apps cannot
pull them.

Never deploy a tag that already exists in the registry under different content.
Container Apps creates a revision only when the template changes, so re-pushing
a moving tag deploys nothing and reports success; `marketagent_image_tag` and
`dashboard_image_tag` both reject `latest`, `main` and `unset` for that reason.

The two images carry their own tag so one can be rolled back without the other
— `make deploy DASHBOARD_IMAGE_TAG=v0.3.3` — but `make deploy` on its own sets
both from the same `IMAGE_TAG`, which is the normal case.

**The schema ships with the code.** Schema files live in
`apps/marketagent/src/marketagent/schema/`, and the first job to write applies
any the database lacks, so there is no separate SQL step before a deploy.

When a release adds a Terraform resource or output, apply it before the
release deploys: `make deploy` reads outputs (the jobs' names,
`database_blob_url`) and stops before changing any running workload if one is
missing.

**Expect the first request after idle to be slow.** `min_replicas = 0` means a
cold start, and the first call can take long enough to look like a failure. That
is the cost design working.

## The daily email

Off until a recipient exists. `make secrets` prompts for it along with the four
API keys, or set it directly:

```bash
az keyvault secret set --vault-name kv-marketagent-dev \
  --name SUMMARY-EMAIL-TO --value 'you@example.com'
```

It lives in Key Vault rather than in Terraform because this repository is
public, and because a value read at runtime takes effect on the next scheduled
run with no redeploy. Note that Resend's shared sender only delivers to the
address that owns the Resend account until a domain is verified.

`--platform linux/amd64` is not optional: Container Apps runs amd64 only, and a
native build on an Apple Silicon host produces an image that crash-loops with an
exec format error and no other clue. The Makefile does it rather than leaving it
to memory.

**Both apps are publicly reachable and unauthenticated**, deliberately: the API
is read-only, so it cannot place a trade or alter a decision however it is
called, and no response carries a secret, any PII, or real money. A shared
bearer sits behind `API_REQUIRE_TOKEN` for when that changes; the proper fix is
Container Apps EasyAuth with Entra, which `azurerm` does not expose.

## Monitoring

Nothing here watches continuously, because nothing here runs continuously. The
apps scale to zero and the jobs run for four minutes a day, so every signal has
to come from something that costs nothing while idle.

**The daily email is the primary channel**, and it reports whether the agent ran
before it reports anything the agent did. This matters more than it sounds:
`No trades were made.` is the same sentence on a day the model held every
position and on a day the 10:30 job died, and the trades, decisions and holdings
sections are all silent in exactly the same way. An `Agent run` table states the
day's runs, their status and any error; a failed, abandoned or absent run also
lands in the **subject line**, which is the part that reaches a phone's lock
screen. The subject is built from stored figures and never by the model, which is
precisely why it can be trusted to carry the warning.

**Azure alerts cover what the application cannot report about itself.** A replica
killed before it opens its `agent_runs` row leaves no evidence at all, and a job
that cannot reach the database may never get far enough to write down why:

| Alert | Fires when | Severity |
| --- | --- | --- |
| `alert-platform-dev-job-failed` | Any job in the shared environment crashes | 1 |
| `*-db-down` | The server stops reporting itself alive | 0 |
| `*-db-storage` | Storage passes 80% of a figure that can never be reduced | 2 |
| `msqa-*` (dedicated workspace only) | Log ingestion approaches the daily cap, past which logging stops | 2 |

They reach whoever holds **Owner** on the subscription, via an ARM role receiver
rather than an address — this repository is public and the tfvars files are
committed, the same reason the summary recipient lives in Key Vault. Set
`TF_VAR_alert_email_address` to add a specific address as well.

Two things worth knowing before changing any of it:

- **`Executions` is a gauge sampled per minute, not a counter.** It reports how
  many executions are in each state right now, so `Total` is
  minutes-times-executions and means nothing — verified against a real run,
  where the 06:00 agent job reported a 5-minute `Total` of 16 for `Running` and
  11 for `Succeeded`. The alerts use `Maximum` over a short window, which is why
  the threshold is `0`.
- **There are no availability tests**, deliberately. A synthetic ping every five
  minutes would keep a `min_replicas = 0` app permanently warm, which costs more
  than the outage it detects.

`agent_runs` remains the ledger underneath all of this — every execution with its
cost, tokens, image tag and error — and the dashboard shows a run still `running`
past the job timeout as abandoned.

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
make install           # pre-commit hooks and Python dependencies
make test              # the Python test suite
make lint              # all pre-commit hooks against every file
make secrets           # prompt for the application secrets, store them in Key Vault
make sql < file.sql    # SQL against the Azure database blob, as one leased transaction
make up / down / logs  # the local stack
make run-agent         # one agent run against the local stack
make build / push      # the linux/amd64 images, and ghcr.io
make deploy            # terraform apply with the built image tag
make fmt               # terraform fmt -recursive
make validate          # terraform init + validate (no Azure credentials needed)
make plan              # terraform init + plan (set ENV=dev|stg|prd, default dev)
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

Nothing costs meaningfully while idle. The database was the exception until it
moved from a PostgreSQL server into a blob — see below.

| | Idle cost |
| --- | --- |
| Container apps | £0 — `min_replicas = 0`, Consumption plan |
| Container app jobs | £0 — billed only while a scheduled run is in flight |
| Key Vault | Effectively £0 — priced per operation |
| Log Analytics / App Insights | Dev shares platform ingestion and caps; costs accrue in `rg-platform-dev`, outside the app resource-group budget |
| Managed identity, resource group | Free |
| Alert rules | A few pence a month each; they read the platform metric store and need no agent or synthetic traffic |
| Database (storage account) | Pennies: a few MB, its versions, and a few hundred operations a day |

### Why the database moved into a blob

PostgreSQL Flexible Server has **no serverless or auto-pause tier**: it billed its
B1ms every hour, about £13 a month, for a database the jobs touch for minutes a
day. Azure SQL serverless *does* auto-pause, and was the original design, but
**this subscription cannot provision Azure SQL in any region it is allowed to
deploy to**:

- The Azure Policy assignment `allowed-locations-dev` has effect **Deny** and
  permits only `westeurope` and `northeurope`.
- The subscription is separately restricted from provisioning Azure SQL in both.

That restriction is regional, not per-SKU: in both regions all 208 Azure SQL
service objectives across all ten editions report `Visible` rather than
`Available`, `Free` and `Basic` included. PostgreSQL was restricted in
`westeurope` but open in `northeurope`, which is why `var.location` defaults
there.

So the database became a file: DuckDB, kept as one blob, leased by whichever
job is writing and read from a cached copy by the API. DuckDB rather than
SQLite because the SQL was written for Postgres, and DuckDB keeps money exact
through `sum()` and speaks most of that dialect. The data is a few MB, the jobs
run a few times a day, and the API is read-only, which is the shape a single
file suits.

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
scale-to-zero compute, "production" here still means paper trading, so a second
and third copy would be three sets of alerts and Key Vaults for nothing. The plumbing is kept so that changing this later is a decision, not a
rebuild.

Dev sets `shared_platform` to the three existing resources in `rg-platform-dev`
(the platform subscription). Terraform reads them as data
sources; their lifecycle, retention, ingestion caps and workspace-wide quota alerts
belong to the platform. Key Vault diagnostics go to the shared
workspace, and the workloads receive the shared Application Insights connection
string. Job alerts and historical log queries filter by environment and job.
The deployment identity needs read access to those resources and permission to
join the environment and manage the dashboard certificate there.
Stg/prd leave `shared_platform` unset and retain dedicated resources.

For an existing dev deployment, follow the
[shared-platform migration notes](docs/shared-platform-migration.md) before apply:
the environment change replaces workloads and changes their default hostnames.

These files are **committed, not gitignored** — see `.gitignore`. Don't put
secrets in them; `gitleaks` (pre-commit and CI) scans every commit as a backstop.

## Secrets

Terraform creates the Key Vault and grants access to it. It deliberately creates
**no secret values** — there is no `azurerm_key_vault_secret` anywhere, so nothing
sensitive reaches Terraform source or state.

`Key Vault Secrets Officer` goes to the object IDs in
`key_vault_administrator_object_ids` (each environment's tfvars), not to
whoever applies, since CD applies as a pipeline identity. One of them populates
the secrets by hand:

```bash
az keyvault secret set --vault-name "$(terraform -chdir=terraform output -raw key_vault_name)" \
  --name OPENAI-API-KEY --value '...'
```

The workload identity holds only `Key Vault Secrets User` — read, not write.

## Database access

The database is `marketagent.duckdb` in the `database` container of
`stmarketagentdev`. The storage account takes **Entra tokens only**
(`shared_access_key_enabled = false`): the managed identity, and the operators
in `key_vault_administrator_object_ids`, have Storage Blob Data Contributor on
the container.

```bash
echo "SELECT name, cash_usd, equity_usd FROM portfolio WHERE is_active" | make sql
make sql < fix.sql   # one transaction; uploads only if every statement succeeds
```

`make sql` runs as you (`az login`) and takes the blob's lease like a job, so it
waits for a running job rather than racing it.

**Schema changes** are numbered, idempotent files in
`apps/marketagent/src/marketagent/schema/`, each recording itself in
`schema_migrations`. The first writer to open a database applies any it lacks.
There are no down-migrations; reversing something is a new numbered file.
`001-schema.sql` is the end state of the old PostgreSQL migrations, and
`002-seed.sql` the companies, pots and watchlists they seeded.

**Restoring a bad write**: the blob is versioned, with 30-day soft delete.
Promote the previous version in the portal, or copy it over the current blob by
version ID.

**Moving off PostgreSQL** was `make import-pg`, run once with the jobs idle. It
attached the server read-only through DuckDB's postgres extension, copied every
table, and uploaded only if every row count matched. It ran on 2026-11-03, and
the server and the import command were removed once the jobs had run on the
blob.

## Scheduling

Job schedules are `agent_cron_expression` (default `30 10 * * *`) and
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
as `main.storage.tf` or `variables.optional.tf` — and `terraform{}`/`provider{}`
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

In CI, `ci-terraform` plans **dev** on every PR touching Terraform through the
shared `terraform-deploy.yml`, using GitHub OIDC (no long-lived secrets) and
commenting the plan on the PR. The **validate** job needs no credentials.

On merge, `cd-terraform` plans dev again. If anything changes it opens a
`terraform-pending` issue, and **applies only when you run it** from the
Actions tab, applying exactly the plan it prints. Stg and prd are never planned
or applied in CI.

The identity, `uai-market-agent-deploy-dev`, and the `dev` GitHub environment
holding `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` come
from [jay-withers/github-repos](https://github.com/jay-withers/github-repos)'s
`state_consumers`, which also creates `rg-marketagent-dev` itself; this
config reads the group by data source. The identity has Contributor and
conditioned RBAC Administrator (any role except Owner, User Access
Administrator and RBAC Administrator) on the group, Reader on the subscription
and join on `cae-platform-dev`, so role assignments inside the group apply
from the pipeline. Run `make apply ENV=dev` locally for a change that
(re)creates the dashboard's managed certificate on the shared environment.

## Repository settings

Branch protection and other repo-level GitHub settings aren't templated here.
This repository is managed centrally by
[jay-withers/github-repos](https://github.com/jay-withers/github-repos)'s
Terraform root module, which requires `terraform / Terraform` and
`terraform-plan` (among others) on every PR.
## Structure

```
apps/
  marketagent/                  # one Python package, five entrypoints, one image
    src/marketagent/
      settings.py               # config; secrets from env, then Key Vault
      db.py                     # DuckDB file in blob: lease per write, cached reads
      schema/                   # numbered, idempotent; applied by the first writer
      models.py                 # domain, risk config, the LLM's output types
      risk.py                   # the deterministic risk engine (pure)
      risklimits.py             # the engine's bounds, from RISK_* variables
      fetch.py                  # shared HTTP, with retries (never on POST)
      fx.py                     # GBP/USD reference rate for dashboard display
      alpaca_api.py             # one credential pair, three Alpaca surfaces
      marketdata.py  news.py    # daily bars; headlines narrowed to the watchlist
      benchmarks.py             # SPY/VT/EWU proxies and cash at 5%
      mailer.py                 # Resend; reports its outcome, never raises
      repository.py             # the writes, sharing a caller's transaction
      telemetry.py              # App Insights; off without a connection string
      queries.py                # the API's reads
      cli.py                    # marketagent api|agent|sync|summary|weekly|sql
      llm/base.py  llm/deepseek_provider.py
      broker/base.py  broker/alpaca.py  broker/dryrun.py
      jobs/agent.py  jobs/summary.py
      api/main.py  api/deps.py  api/routers/
    tests/                      # no network, no Azure, no credentials
    Dockerfile
  dashboard/                    # Vite + React + TS -> unprivileged nginx:8080
    src/  nginx/  public/config.json.template
    docker-entrypoint.sh        # renders config.json from $API_ORIGIN at start
    Dockerfile
docker-compose.yml
.env.example
terraform/
  main.tf                       # resource group
  main.identity.tf              # user-assigned managed identity
  main.observability.tf         # Log Analytics, App Insights, diagnostic settings
  main.alerts.tf                # action group, alert rules, budget
                                #   (what each rule watches: locals.alerts.tf)
  main.key-vault.tf             # vault and RBAC (never the secret values)
  main.storage.tf               # the database's storage account, container and grants
  main.container-apps-env.tf    # Container Apps environment
  main.container-apps.tf        # api and dashboard
  main.container-apps-jobs.tf   # agent, daily-summary and weekly-review jobs
  locals*.tf  variables*.tf  outputs.tf  data.tf  versions.tf
  README.md                     # generated by terraform-docs
  .tflint.hcl  .terraform-docs.yml
  backends/{dev,stg,prd}.hcl    # partial backend config per environment
  environments/{dev,stg,prd}.tfvars
scripts/
  Set-KeyVaultSecrets.ps1       # prompts for the Key Vault values
  check-tf-standards.sh         # enforces the file-layout convention
  tflint-per-env.sh  checkov-per-env.sh
  hadolint.sh                   # wraps hadolint, resolving DOCKER_HOST first
docs/deployment-plan.md         # the agreed plan; complete
.github/workflows/
  ci-pre-commit.yml             # lints all files on PRs to main
  ci-terraform.yml              # terraform validate + dev plan (commented on the PR)
  cd-terraform.yml              # plans dev on merge; applies on a manual run
  ci-container-build.yml        # builds both app images on PRs (no push)
  cd-tag.yml                    # auto-tags on merge to main, then publishes
  cd-publish.yml                # builds + pushes the app images for a tag, then deploys and smoke-tests them
.pre-commit-config.yaml
commitlint.config.js
renovate.json
Makefile
```

## What is still to come

The infrastructure and the application are both done and deployed. What remains
is not construction:

- **The experiment itself.** Three to six months of daily runs, then the
  comparison against the S&P 500, a global index, a UK proxy and cash at 5%.
  The benchmark arms are already recorded each evening.
- **Authentication on the two apps.** Both are publicly reachable, deliberately
  — the API is read-only and no response carries a secret, PII or real money.
  The proper fix is Container Apps EasyAuth with Entra, which `azurerm` does
  not expose and which would need `azapi`.
- **Cheaper filtering, if it ever matters.** One filter call per
  article/ticker pair is about 110 calls a run. Batching would cut that to
  roughly six, but at the measured £4/month it is a latency argument rather
  than a cost one.
