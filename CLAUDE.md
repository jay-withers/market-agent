# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Azure infrastructure for **MarketAgent**, an AI paper-trading and investment
research platform: an LLM recommends BUY/SELL/HOLD from news and market data, a
deterministic risk engine decides what's actually permitted, and approved orders
run against Alpaca's paper-trading broker. No real money, ever — the experiment
mirrors Alpaca's $100,000 paper account and compares it with passive indexes and
a savings benchmark.

**The application is written, deployed and working.** `apps/marketagent/` is the
Python package (API, agent job, summary job) and `apps/dashboard/` is the React
front end; both run in Container Apps from public ghcr.io images, and
`docker-compose.yml` runs the same code locally. All eight steps of
`docs/deployment-plan.md` are done.

Verified live on 2026-09-03, image tag `8d2788f`:

- The agent job ran to completion in Azure — 84 articles, 50 relevant, 9
  decisions, 3 trades, **$0.19, 4m13s** — with `trigger = schedule` and the
  image tag recorded on the `agent_runs` row. The risk engine refused two of
  the model's four BUYs on `daily_trade_limit`.
- The summary job wrote a `daily_performance` row, four benchmark arms and a
  Sonnet-written `daily_summaries` row in 44 seconds. A recipient is now
  configured in Key Vault, so `email_status` is no longer `skipped`; the
  2026-09-03 send itself failed on the non-ASCII address below.
- `/readyz` returns `{"status":"ok","database":true}`, which is the **managed
  identity and the Entra token path to PostgreSQL** both working: those two,
  plus the images running on amd64, were the three code paths that had never
  executed anywhere before the deploy.

**A first request after idle is slow.** `min_replicas = 0` means a cold start,
and the first call can take long enough to look like a failure — that is the
cost design working, not a fault. Retry before investigating.

**Both apps are publicly reachable and unauthenticated.** Deliberate: the API
is read-only, so it cannot place a trade or alter a decision however it is
called, and no response carries a secret, PII or real money.

This repo began as a generic Azure Terraform module template and was converted;
if something looks like leftover template scaffolding, it probably is.

This file is the rules, one line each. The reasoning behind them, and the
incidents that made them, are in [docs/infrastructure.md](docs/infrastructure.md), [docs/application.md](docs/application.md) and
[docs/operations.md](docs/operations.md): read the linked section before changing the thing a rule
protects.

## Commands

```bash
make install           # pre-commit hooks and Python dependencies (re-run after a container rebuild)
make test              # the Python suite; make test-db adds the Postgres-backed tests
make lint              # all pre-commit hooks against every file
make up / down / demo  # the local docker compose stack (demo seeds fake data)
make run-agent         # one agent run against the local stack
make sql               # every file in sql/, in order, against the Azure database
make validate          # terraform init -backend=false + validate
make deploy IMAGE_TAG=vX.Y.Z
```

`make` with no target lists the rest ([commands](docs/operations.md#commands)).

## Rules

### Cost and region ([cost](docs/infrastructure.md#the-cost-constraint-drives-most-of-the-design), [region trap](docs/infrastructure.md#the-database-and-the-region-trap))

- **Nothing may bill meaningfully while idle.** That rules out ACR, a workload
  profile, `min_replicas > 0`, availability tests, extra diagnostic settings
  and `AllMetrics`. Keep the log caps (`daily_quota_gb = 0.15`). The database
  stays at 32 GB with `auto_grow_enabled = false`.
- **PostgreSQL in northeurope was forced, not chosen.** Azure SQL can't be
  provisioned in any region the policy allows. Probe the capabilities APIs
  before touching `var.location` or the database.

### Infrastructure ([layout](docs/infrastructure.md#terraform-layout), [naming](docs/infrastructure.md#naming), [PostgreSQL](docs/infrastructure.md#postgresql-specifics), [security](docs/infrastructure.md#security-posture))

- `scripts/check-tf-standards.sh` enforces the file layout, and is shared
  verbatim with other repos. Variables are split into `variables.required.tf`
  and `variables.optional.tf`.
- Names use the naming module's `.name`, never `.name_unique`. Every name
  carries `project_name` (max 15 chars). Pot names are max 6 chars (the agent
  job hits 32).
- The Postgres Entra admin is a named human by **UPN, never display name**.
  It comes from `TF_VAR_POSTGRES_ENTRA_ADMINISTRATOR`. Password auth is off.
- Terraform creates **no `azurerm_key_vault_secret`, no `random_password` and
  no native Key Vault reference.** Secrets are set out of band
  (`make secrets`).
- Grant RBAC to explicit object IDs, never to "whoever applies".
  `cd-terraform` applies role assignments inside `rg-marketagent-dev`. One
  elsewhere, or the dashboard's managed certificate, needs a local
  `make apply ENV=dev`.
- Run `checkov` after adding a resource, and add inline skips for exactly what
  fires ([Checkov](docs/infrastructure.md#checkov-skips)).

### The database ([running SQL](docs/infrastructure.md#running-sql-by-hand), [testing SQL](docs/operations.md#testing-the-sql))

- Migrations are numbered, idempotent `sql/0NN-*.sql` files. **`make sql`
  isn't part of `make deploy`: apply a migration the code needs before
  deploying it.**
- `demo/seed.sql` stays out of `sql/`. `make sql` runs all of `sql/` against
  Azure.
- `pgaadauth` exists only in the `postgres` database. Firewall rules need
  public access.
- No function in `repository.py` commits; the caller owns the transaction.

### Money and the risk engine ([application package](docs/application.md#the-application-package))

- **Money is `Decimal`, never `float`**, rounded towards zero. The only floats
  are the LLM's `suggested_amount_usd` and `queries.py`'s display values.
- `risk.py` is pure: no I/O, no clock, no randomness. Gates come before caps.
  Headroom is clamped at zero.
- **An order POST is never retried.** `client_order_id` is deterministic per
  decision.
- Risk limits change only when a person edits `risklimits.py`. Nothing applies
  the model's proposals.

### The model ([LLM cascade](docs/application.md#the-llm-cascade), [agent](docs/application.md#the-agent-job-and-what-the-live-apis-actually-do), [pots](docs/application.md#the-pots))

- Pydantic docstrings and `Field(description=…)` are **prompt text**. Put
  maintainer notes in `#` comments.
- **Anything added to `_prompt()` must also be stored in `prompt_context`**,
  or an `ai_decisions` row stops being replayable. Bump `PROMPT_VERSION`
  when a prompt changes.
- Accumulate cost per call, never from token totals. `MAX_RUN_COST_USD`
  is checked as each call lands.
- Reasoning calls use `REASONING_TIMEOUT_SECONDS` (180s), not `fetch.py`'s
  30s.
- The database is the list of pots, and it must match Terraform's
  `local.pots`. Secret names are derived (`ALPACA-API-KEY-TECH`).

### Jobs and emails ([summary](docs/application.md#the-summary-job), [weekly](docs/application.md#the-weekly-review), [integrity checks](docs/application.md#the-data-integrity-checks), [monitoring](docs/infrastructure.md#monitoring-and-alerting))

- **Every figure in an email and every subject line comes from the
  database**, never from the model. The model only writes commentary.
- Integrity checks are computed and rendered by us, and report pass or fail
  every week.
- The weekly review (22:00 Sunday UTC) must run after that day's summary
  (21:00). Crons are UTC ([scheduling](docs/infrastructure.md#scheduling)).
- A mail failure never loses a summary. Recipients live in Key Vault
  (`SUMMARY-EMAIL-TO`) and must be plain ASCII.
- `Executions` is a gauge. Alert on `Maximum`, never `Total`.

### API and dashboard ([API](docs/application.md#the-api-and-the-local-stack), [dashboard](docs/application.md#the-dashboard))

- The API is **read-only**. `/healthz` must not touch the database, `/readyz`
  must, and both sit outside the bearer gate.
- The dashboard is gated by `DASHBOARD-PASSCODE`, fetched by its entrypoint
  from Key Vault. Its alphabet excludes `;` `,` `"` `\`, whitespace and
  non-ASCII. No Entra sign-in for users: that's an estate-wide decision.
- Render `assessment` as text, never `body_html` (model output, XSS).
- The chart palette's slot order is the colourblind-safety mechanism. The
  table view is a required accessibility channel.

### Images and deploys ([deploy](docs/operations.md#commands), [CI](docs/operations.md#ci))

- `make deploy` is az cli and needs an explicit tag. `env` is under
  `ignore_changes`, so a `common_env` change lands only through `make deploy`.
- Images run as uid 10001. Both `uv sync` calls use `--no-editable`. Docker
  compose uses no bind mounts (docker-outside-of-docker).
- Keep `marketagent selfcheck` side-effect free and complete. `scripts/smoke.sh`
  is shared verbatim with five repos.
- Tags are immutable (`vX.Y.Z` and short SHA). After a release, check the
  registry, not the green run.

### CI and repo settings ([CI](docs/operations.md#ci), [repo settings](docs/operations.md#github-repo-settings))

Thin callers of `jay-withers/workflows`, pinned by SHA. The required checks
(set in github-repos) are `pre-commit / Pre-commit`, `test / Test`,
`terraform / Terraform`, `terraform-plan`, `Layout` (ci-dashboard's
Playwright layout suite) and `container-build`. ci-python needs
`extras: dev`. Renovate
extends `github>jay-withers/renovate`. `cd-terraform` plans on merge and
applies only when run by hand.

## Commit messages

Commits must follow [Conventional Commits](https://www.conventionalcommits.org/) — enforced by commitlint at commit-msg time. Examples: `feat: add container apps jobs`, `fix: correct key vault name length`, `chore: bump azurerm provider`.
