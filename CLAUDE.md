# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Azure infrastructure for **InvestAgent**, an AI paper-trading and investment
research platform: an LLM recommends BUY/SELL/HOLD from news and market data, a
deterministic risk engine decides what's actually permitted, and simulated trades
run against a paper-trading broker. No real money, ever — the experiment is
whether the AI beats a passive index or a savings account over 3–6 months with a
notional £500.

**The application is part-written.** `apps/investagent/` holds the settings, the
database layer, the domain models and the risk engine with its tests; there is
still no LLM client, no broker client, no jobs, no API, no dashboard, no
Dockerfile and no image build pipeline. Both container apps and both jobs still
run public Microsoft quickstart images as placeholders, which also means the two
apps are publicly reachable unauthenticated pages — authentication belongs with
the real API. `docs/deployment-plan.md` is the agreed plan for the rest and is
worth reading before adding to it.

This repo began as a generic Azure Terraform module template and was converted;
if something looks like leftover template scaffolding, it probably is.

## Dev container

The repo is built around the dev container at `.devcontainer/devcontainer.json`,
which uses the image `ghcr.io/jay-withers/dev-containers/terraform`. It provides
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
scheduled container app jobs (`agent`, `daily-summary`), and a PostgreSQL
Flexible Server plus database. Names come from `Azure/naming/azurerm`.

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
  explicitly — `data.azurerm_client_config` cannot report it. `object_id`,
  `principal_name` and `principal_type` are **hardcoded to a named human** in
  `main.database.tf`, not variables: the administrator is then the same principal
  whoever or whatever applies. Changing it is a code edit, not a tfvars change.
  An object ID is an identifier rather than a credential, so it is safe in this
  public repo.
- **`principal_name` must be the UPN, not the display name** — ARM makes it the
  PostgreSQL role name verbatim. Registered as `Jay Withers`, the role really is
  called `Jay Withers`, and connecting as `jay.withers@appvia.io` fails with
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
psql-marketagent-dev            appi-marketagent-dev  caj-marketagent-dev-agent     (25/32)
psqldb-marketagent-dev          uai-marketagent-dev   caj-marketagent-dev-summary   (27/32)
```

The daily summary job is named `summary`, not `daily-summary`: the latter would be
33 characters against the 32 container app jobs allow, and the naming module
truncates silently. Its container is still `daily-summary`.

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
hardcoded in `main.database.tf` (object ID, principal name and type), so a CI
apply no longer silently makes the OIDC service principal the only way in — the
trap that an earlier "default to whoever applies" version carried. An Entra group
would survive personnel change, at the cost of `principal_type = "Group"` and a
group created out of band.

Terraform owns the Key Vault and its RBAC but creates **no**
`azurerm_key_vault_secret`: values are set out of band with
`az keyvault secret set` by whoever holds `Key Vault Secrets Officer` (granted to
the deploying principal automatically, plus `key_vault_administrator_object_ids`).
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

## The application package

`apps/investagent/` is one Python package, one image, three entrypoints
(`api`, `agent`, `summary`). They share the risk engine, the database layer, the
broker client and the LLM client, so splitting them into three images would mean
three builds of near-identical layers. Deviates from the brief's suggested
`apps/{api,agent,dashboard}` deliberately; the dashboard is genuinely separate
and gets its own image.

Written so far: `settings.py`, `db.py`, `models.py`, `risk.py`, and tests for
the last two. Dependencies in `pyproject.toml` are added as the modules needing
them land rather than declared up front.

- **Money is `Decimal`, never `float`**, matching the `NUMERIC(18,4)` columns.
  `models.money()` quantizes to four places and always rounds **towards zero**,
  because every caller is a limit or headroom against one and rounding up could
  approve a trade a hair over a cap. `pytest` runs with
  `filterwarnings = ["error"]` so a float sneaking into Decimal arithmetic fails
  rather than warns.
- **The one place a float is allowed is the LLM's own output.** A
  `Recommendation.suggested_amount_gbp` arrives as a JSON number and is coerced
  through a float, which is fine because it is an opinion, not an accounting
  figure — the risk engine quantizes it before using it, and every exact amount
  in the system originates from the engine or the database instead.
- **`risk.py` is pure** — no I/O, no clock, no randomness. `trades_today` is
  passed in because the engine cannot count the `trades` table itself. That
  purity is what makes an `ai_decisions` row replayable months later.
- **Gates before caps.** Gates are conditions no trade size can satisfy and they
  short-circuit; caps each yield a maximum and the smallest wins. Caps are
  evaluated in a fixed order and the *first* minimum wins, so ties are
  deterministic — `recommended_amount` is first, so an unclamped approval is
  reported as bound by the recommendation rather than by a limit that tied.
- **`available_cash` is dominated whenever `max_total_exposure_pct < 100`**:
  exposure headroom is `pct x total - invested` while cash is
  `total - invested`, so the former is always smaller. It is kept as a backstop
  for a 100% ceiling and for a self-inconsistent state, not because it is
  expected to bind. A test asserts the domination so nobody "fixes" the
  apparent redundancy.
- **Headroom is clamped at zero.** A price rise alone can push a position past
  its cap without any trade, and a negative cap would otherwise read as the
  tightest one and be reported as an approved amount.
- **A refusal never repeats a constraint in `reasons`.** The refusal path must
  not append a fresh entry for a cap already recorded, or
  `ai_decisions.risk_verdict` ends up holding the cap with its value and the
  same name again with none. There is a regression test.
- `settings.secret("ANTHROPIC-API-KEY")` reads `$ANTHROPIC_API_KEY` first and
  only then Key Vault. Env-first is what makes `docker compose` work with no
  Azure at all, and it is why Terraform owns no Key Vault references — a
  revision carrying one hard-fails when the secret is absent, which all four
  currently are. The Azure SDKs are imported *inside* the functions that need
  them so the models, the engine and the tests need neither the SDK nor a
  credential.
- `db.py` authenticates with an Entra token as the password. A
  `psycopg.Connection` subclass fetches one inside `connect()` so each new
  connection gets a fresh token, and `max_lifetime=1800` retires connections
  well inside a token's ~60 minutes. The pool is built lazily, so importing the
  module doesn't demand a credential.

### The LLM cascade

Two stages, two models, **two different request shapes** — and the shapes are
the trap, because getting one wrong is a 400 from the API at 06:00 UTC in a
container, not a wrong answer here.

- `claude-haiku-4-5` (`FILTER_MODEL`) screens news for relevance. It predates
  the 4.6 family, so `output_config={"effort": ...}` **errors** and adaptive
  thinking does not exist for it. The filter stage therefore sends neither.
- `claude-sonnet-5` (`ANALYSIS_MODEL`, $2/$10 per MTok) produces the
  assessment, with `thinking={"type": "adaptive"}` and
  `output_config={"effort": "high"}`. It rejects `budget_tokens`,
  `temperature`, `top_p` and `top_k` with a 400. **Sonnet is the user's
  explicit choice** — an earlier draft defaulted to `claude-opus-5` and was
  overruled; don't "upgrade" it back.
- Neither model accepts an assistant prefill.
- `_reasoning_params()` in `llm/anthropic_provider.py` is what keeps the two
  apart, by prefix match against `LEGACY_MODEL_PREFIXES` rather than an exact
  list, so a dated snapshot id can't silently take the wrong branch.

**`messages.parse()` sends your Pydantic model to the model.** It builds the
JSON schema from the class, and the **class docstring becomes the schema's
`description`** while `Field(description=...)` becomes each property's. So
those strings are prompt text, not internal documentation — notes for a future
maintainer go in a `#` comment instead. The original `Recommendation` docstring
explained Python `Decimal` coercion and was being sent to the model verbatim.

For the same reason `Recommendation.suggested_amount_gbp` is a **`float`**, the
only place in the system money is not a `Decimal`: declared as `Decimal` it
rendered as a three-branch `anyOf` — number, string with a Decimal regex, or
null — which is a worse thing to hand a model than a plain number. `money()`
converts via `str()` so a float's binary artefacts never reach a stored figure,
and it accepts `float` for that one caller only.

Note that `output_format=` on the `messages.parse()` helper is current, while
the `output_format` *parameter* on `messages.create()` is deprecated in favour
of `output_config={"format": ...}`. They are different things, and `parse()`
merges its own `format` into `output_config` alongside the `effort` we set.

`tests/test_llm.py` passes a fake client and asserts what this code calls;
`tests/test_llm_wire.py` drives the **real** SDK through an `httpx2`
`MockTransport` and asserts the request body that would go on the wire. The
second layer is what caught both schema problems above, so keep it — and note
it is `httpx2`, not `httpx`: the 1.x SDK moved, and passing an `httpx.Client`
raises a `TypeError`.

### The agent job, and what the live APIs actually do

`jobs/agent.py` is the loop: state, prices, news, cheap filter, analysis, risk
engine, paper trade, persist — all inside one `agent_runs` row opened before
any work, so a crash leaves evidence. `llm` and `broker` are injectable, which
is the only way to exercise the loop without spending money and placing orders.

**The LLM cost, measured on a real run rather than estimated.** One complete
run over the ten-name watchlist: 82 articles fetched, ~110 filter calls (one per
article/ticker pair — an article tagged with three watchlist names costs three),
46 relevant, 9 analyses, **85,643 input and 11,658 output tokens, $0.18, and
4.1 minutes wall clock**. At one run a day that is about **£4/month**, against
the database's £13.

An earlier figure of $0.43 in this file was wrong: it came from a rehearsal with
a stubbed LLM whose token counts were invented, and it overstated the real cost
by more than double. Quote the measured figure, not that one.

Two consequences. Batching several articles into one filter call would cut ~110
calls to about 6, but at £4/month the saving is small — it is a latency argument
now more than a cost one. And 4.1 minutes sits comfortably inside the job's
`replica_timeout_in_seconds = 1800`, so the timeout is not the constraint it
looked like it might be.

**Cost must be accumulated per call, never derived from token totals.** The two
stages use different models at different rates, so pricing a mixed token total
at either rate is simply wrong — it read 30% high when the filter's Haiku
tokens were priced as Sonnet ($0.566 against a true $0.434). The token columns
still hold the mixed totals, which is fine because they are counts.

**"Submitted, no fill" is the normal outcome of a scheduled run.** The agent
runs at 06:00 UTC and the US market opens at 14:30, so a market order sits
`accepted` for eight hours. Consequences: `trades.quantity` had to become
nullable (a notional order never names a quantity — Alpaca derives it), a
`client_order_id` column was added so a fill can be reconciled later, and cash
and positions move **only** on an actual fill. `002-trade-submission-fields.sql`
is that correction.

**An order POST is never retried.** `fetch.post_json` deliberately does not
share `get_json`'s retry: a 503 that executed before failing to answer is
indistinguishable from one that did not, and a retried order is a duplicated
trade. `client_order_id` is deterministic per decision so a *manual* retry is
idempotent at the broker and updates the existing `trades` row.

Live API facts, all verified rather than assumed:

- **The Alpaca paper account holds $100,000 with $400,000 buying power**, against
  a notional £500. It will happily execute orders 800x too large, so its balance
  is not a safety net — the risk engine and our own `portfolio` table are the
  only things bounding size, and `positions`/`portfolio` are the ledger.
- **`feed=sip`, not `iex`.** This account has SIP, the consolidated tape. On
  NVDA, `iex` reported 4.9M shares against SIP's 157M and a close of 224.435
  against 224.41. Set explicitly so losing the subscription fails the run
  instead of silently changing which prices the experiment ran against.
- **The FX endpoint is `https://api.frankfurter.dev/v1/latest`.** The
  `frankfurter.app` host 301s and the path needs the `/v1`; without it you get a
  **404 body with an HTTP 200 shape**, which arrives as a missing dict key
  rather than an HTTP error. `fx.py` reports that case explicitly.
- **News is tagged with every symbol an article mentions** — one live article
  listed ten. `news.py` narrows to the watchlist before storing, which is what
  keeps `news.tickers` meaningful.

`db.py` picks its authentication from whether `POSTGRES_PASSWORD` is set: empty
means an Entra token (Azure has no password at all), set means ordinary password
auth for the local Postgres that `docker compose` runs. Without that split the
local loop could not connect.

`repository.py` is a deviation from the plan's file layout, which lists no such
module. The alternative is the same SQL in `jobs/agent.py` and again in the
API's routers. Every function takes a connection rather than reaching for the
pool, so the agent can put a decision and its trade in one transaction — a
trade with no decision behind it would be unauditable.

Tests never touch Azure or the network: `tests/conftest.py` sets every secret as
an environment variable and **deletes `KEY_VAULT_URI`**, so a missing one fails
rather than falling through to a real call, and it clears both `settings()` and
`secret()` caches around each test. HTTP is faked with `httpx.MockTransport` via
`tests/helpers.py`. Note `tests/` is a package, so helpers import as
`tests.helpers` — a bare `from conftest import ...` does not resolve.

### The summary job

`jobs/summary.py` runs at 21:00 UTC and does the thing the agent could not:
the agent submits at 06:00 and the US market opens at 14:30, so **this is where
a fill becomes known**, cash and positions move, and the day gets a valuation.
Then benchmarks, a written commentary, an email, and a `daily_summaries` row.

**Every figure in the email comes from the database.** The model is given the
numbers as a Markdown table it is told not to restate, and writes only the
commentary; the subject line is built from the stored total, not by the model.
Nothing it writes can become a reported balance.

That rule is only as good as the table. The first version listed a
reconciliation count and no trades — and that count is *zero* on a dry run,
because a simulated trade never reaches a broker. The model then reported,
accurately from what it had been given, that cash and positions were unchanged
on a day three trades had executed. The table now states the day's trades
first and spells out what `simulated` means. `tests/test_summary.py` is the
regression.

- **Benchmarks live in `companies` with `is_benchmark = true`.** `prices.ticker`
  references `companies`, so storing a close for SPY, VT or EWU failed with a
  foreign key violation until they existed. A flag rather than reusing
  `is_active = false`, which means "retired from the watchlist" — a benchmark
  never was on it, and the dashboard has to tell them apart to label a proxy as
  a proxy. `004-benchmark-companies.sql`.
- **The index arms need no FX.** Value is `notional x close_now / close_at_inception`,
  a pure ratio in which the currency cancels. An earlier version applied
  today's rate to both ends and produced a series that moved with sterling
  rather than with the index.
- **A benchmark with no price is omitted, not flat-lined.** £500 on the chart
  reads as "the index did nothing", which is a different and wrong claim from
  "we have no data".
- **Cash-at-5% compounds daily**, because the chart is read daily and an annual
  step function would be nonsense.
- **A mail failure never loses the summary.** `mailer.send` returns a status
  rather than raising; the row is stored either way and the dashboard renders
  from it. `SUMMARY_EMAIL_TO` is empty by default, so sending is opt-in — a job
  that emails on every development run is worse than one switched on
  deliberately.
- Reconciliation commits per trade: one order the broker cannot answer for must
  not roll back the fills already applied.

### The API and the local stack

`api/` is **read-only**. Every mutation belongs to the agent and summary jobs,
so this process cannot place a trade or alter a decision however it is called —
which is most of why it is comfortable being publicly reachable. `/api/*` is
guarded by an optional shared bearer (`API_REQUIRE_TOKEN`, off by default); the
real fix is Container Apps EasyAuth with Entra, which `azurerm` does not expose.

- **`/healthz` must not touch the database and `/readyz` must.** Container Apps
  restarts a container on a failed liveness probe, so a database blip that
  failed `/healthz` would restart every replica and turn an outage into a crash
  loop. The health endpoints are also deliberately **outside** the bearer gate —
  a probe cannot present a token.
- **`queries.py` returns money as `float`**, the one place the Decimal rule is
  relaxed. It is the display boundary: a browser charts the values and throws
  them away, nothing computes with them, and no result goes back to the
  database. A `Decimal` would serialise as a JSON string and make every chart
  awkward for no gain.
- The overview's position valuation uses the **most recently recorded** FX rate,
  not `max()`. The first version used `max()`, which is wrong in a way that
  worsens: it locks onto the highest rate ever seen and never moves.
- Every `limit` is bounded. An unbounded one is how a read-only API becomes a
  denial of service.

**The containerised agent gets its secrets from Key Vault, not a `.env`.** The
image has no `az`, so `DefaultAzureCredential` has nothing to fall back to and
a container cannot authenticate on its own. `make run-agent` therefore mints a
short-lived, Key Vault-scoped token with the caller's `az login` and passes it
in as `AZURE_KEYVAULT_TOKEN`, which `settings.credential()` wraps in
`StaticTokenCredential`. It lasts about an hour and unlocks nothing but Key
Vault — a far better thing to hand a container than four long-lived API keys in
a file. That credential **refuses a scope it was not issued for**, because
`credential()` also serves Postgres token auth and a Key Vault token used there
fails as `password authentication failed`, which this file already records as
one of the hardest errors here to read correctly. `.env` still works and
`.env.example` documents it, for anyone with no Azure access.

**`docker compose` here has no bind mounts, on purpose.** The Docker daemon runs
on the *host* (docker-outside-of-docker), so a relative bind mount resolves
against the host filesystem — `./sql` fails from inside the dev container with
"path is not shared from the host", because `/workspaces/market-agent` does not
exist there. Build contexts are sent by the *client* and work from either side,
so `docker/Dockerfile.db` bakes the schema in instead. Two consequences worth
keeping: published ports land on the **host**, so from inside the dev container
reach a service at its bridge IP or `host.docker.internal`; and the db image
copies `sql/0*.sql` only, because `grant-uai-access.sql` calls
`pgaadauth_create_principal` and `\connect`s to a database named after the
Azure server — as an initdb script it would fail the whole initialisation.

**`uv sync` installs the project editable by default**, which produces a `.pth`
pointing at `/app/src`. The runtime stage copies only the virtualenv, so the
image failed with a bare `No module named 'investagent'` from a venv that looked
complete. `--no-editable` on both syncs is the fix.

The image runs as **uid 10001, numerically** — `USER investagent` trips
hadolint's DL3066, and a runtime enforcing `runAsNonRoot` has to resolve the
user before the container starts without being able to read the image's
`/etc/passwd`.

Dockerfiles are linted by the local `scripts/hadolint.sh`, **not** the upstream
`hadolint-docker` hook. That hook calls `docker system info`, which fails
whenever `DOCKER_HOST` is unset — and pre-commit invoked from `git commit`
inherits a non-login shell, where `/etc/profile.d/10-docker-host.sh` has not
run. It blocked a commit with a wall of JSON on nothing more than how the shell
was started. The wrapper resolves the socket the same way that profile snippet
does, then fails loudly if the daemon is still unreachable.

**`ruff-format`'s upstream hook includes `markdown` in its `types_or`**, so it
reformats Python code blocks inside `.md` files — it rewrote the aligned
comments in `docs/deployment-plan.md` on its first run. The hook is pinned to
`types_or: [python, pyi]` in `.pre-commit-config.yaml` for that reason; don't
drop the override. Ruff's own config lives in `apps/investagent/pyproject.toml`
and is found by walking up from each file, so nothing points at it from the
pre-commit config.

## Commands

`make` with no target prints the self-documenting help (the default goal).

```bash
make install           # install pre-commit hooks and Python dependencies
make test              # run the Python test suite (pytest)
make lint              # run all pre-commit hooks against every file
make fmt               # terraform fmt -recursive
make validate          # terraform init + validate (no Azure credentials)
make plan              # terraform init + plan (set ENV=dev|stg|prd, default dev)
make apply             # terraform init + apply (set ENV=dev|stg|prd, default dev)
```

`make test` runs the Python suite. There are no *Terraform* tests — those were
removed at the user's request — and `make validate` is the credential-free
Terraform check.

`make install` is expected to be re-run after a dev container rebuild, not only
after a clone: `uv` installs into `~/.local/bin`, which is the container's
writable layer and does not survive one. `az login` and `gh auth login` go the
same way, and so does anything `apt-get install`ed by hand — `psql` was missing
after the last rebuild despite the deployment plan recording it as present.

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
