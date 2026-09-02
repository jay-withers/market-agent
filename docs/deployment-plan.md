# Deploy the InvestAgent application from local

## Context

The repo is infrastructure-only. All eleven Azure resources for `dev` are deployed
and healthy, but both container apps serve `mcr.microsoft.com/k8se/quickstart` on
port 80 and both jobs serve `quickstart-jobs` — there is no application code
anywhere, no Dockerfile, no `pyproject.toml`, no `package.json`. Key Vault
`kv-marketagent-dev` is **empty**: not one secret has been set.

This plan writes the application (all four workloads), the database schema, and
the Terraform changes that point the four workloads at real images — then
deploys it by hand from this machine. No CI image pipeline; `deploy from local
for now` is taken literally, and the existing `ci-terraform` workflow keeps
validating and planning as it does today.

Decisions taken up front: Anthropic Claude for analysis; Alpaca for market data,
news *and* paper execution (one credential pair covers all three); Resend for the
daily email; ghcr.io packages **public** so nothing needs a pull token; Docker
reached through the host daemon via a devcontainer feature.

### Two environment facts that shape the mechanics

- **This devcontainer has no Docker yet** (no CLI, no socket) but does already
  have Python 3.12, Node 24, `psql` 16, `pwsh` 7.6 and `az` (logged in as you).
  You've added `docker-outside-of-docker` to the dev container image separately —
  but this repo pins that image by digest (`:latest@sha256:aa9938…`), and
  upstream `latest` *and* `v0.11.0` both still resolve to that same digest. So
  the change isn't published yet, and until the pin moves, a rebuild changes
  nothing. Tracked as a prerequisite, not as work in this plan.
- **The host is arm64** (`linuxkit`/`aarch64` — Docker Desktop on Apple Silicon)
  and **Container Apps runs linux/amd64 only**. Every build must pass
  `--platform linux/amd64` or the revision will crash-loop with an exec format
  error. This is baked into the Makefile rather than left to memory.

## Repository layout

```
apps/
  investagent/            # one Python package, one image, three entrypoints
    pyproject.toml
    Dockerfile
    src/investagent/
      settings.py         # env-first, Key Vault fallback
      db.py               # psycopg pool with Entra token auth
      models.py           # pydantic domain + LLM output models
      risk.py             # deterministic risk engine (pure)
      fx.py  marketdata.py  news.py  benchmarks.py  mailer.py
      llm/base.py  llm/anthropic_provider.py
      broker/base.py  broker/alpaca.py
      api/main.py  api/routers/*.py
      jobs/agent.py  jobs/summary.py
      cli.py              # `investagent api|agent|summary`
    tests/
  dashboard/              # React + TS + Vite -> nginx
    package.json  Dockerfile  nginx/default.conf  public/config.json.template
    src/
sql/                      # numbered migrations alongside the existing grant file
docker-compose.yml        # local Postgres + api + dashboard
```

The brief suggests `apps/{api,agent,dashboard}`. Deviating deliberately: the API,
agent and summary share the risk engine, DB layer, broker client and LLM client,
so they are **one Python package in one image** differing only by container
`args`. Three images would mean three builds and three pushes of near-identical
layers for no benefit. Two images total.

## Terraform changes

The enforced file-layout convention (`scripts/check-tf-standards.sh`) still
applies — new `locals`/`variable`/`output` blocks go in matching files.

- **`variables.optional.tf`** — add `image_registry` (default
  `ghcr.io/jay-withers/market-agent`) and `image_tag`. **`image_tag` must be an
  immutable tag, not `latest`**: Container Apps only creates a new revision when
  the template changes, so re-pushing `latest` silently deploys nothing. The
  Makefile passes `-var image_tag=$(git rev-parse --short HEAD)`.
- **`locals.container-apps.tf`** — replace `placeholder_app_image` /
  `placeholder_job_image` with computed refs; add `api_target_port = 8000` and
  `dashboard_target_port = 8080`; add `POSTGRES_USER` (the UAI name — the DB role
  name) and `POSTGRES_PORT` to `common_env`, which today omits both.
- **`main.container-apps.tf`** — `target_port` from locals instead of hardcoded
  80; `command = ["investagent"]` with `args = ["api"]`; a liveness and readiness
  probe on `/healthz`; the dashboard gets `API_ORIGIN` pointing at the API's
  ingress FQDN.
- **`main.container-apps-jobs.tf`** — `args = ["agent"]` / `["summary"]`. No
  `registry` or `secret` block anywhere, which is the whole point of public
  packages.
- **`outputs.tf`** — add `agent_job_name` and `summary_job_name` (the deploy
  targets need them to trigger a manual run) and `identity_client_id`. Nothing
  else; outputs earn their place.
- Run `checkov` afterwards and add inline skips for **exactly** what fires, per
  the existing convention. Then `make lint` twice — the first run rewrites
  `terraform/README.md` via terraform-docs and reports failure.

## Managed-identity Postgres access

The server is Entra-only; no password exists. In `db.py`:

```python
_cred = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])   # in Azure
_cred = DefaultAzureCredential()                                            # local dev
token = _cred.get_token("https://ossrdbms-aad.database.windows.net/.default")
```

The token is the **password**; the username is the DB role. In Azure that is
`uai-marketagent-dev`; locally it is your UPN `jay.withers@appvia.io` — and per
CLAUDE.md's trap, the UPN, never the display name.

Token expiry is the thing to get right for the long-lived API pool. A
`psycopg.Connection` subclass injects a fresh token in `connect()`, and the pool
is built with `connection_class=` that subclass plus `max_lifetime=1800` so
connections recycle well inside the token's ~60 minute life. `azure-identity`
caches and refreshes internally, so the per-connect call is cheap. Pool freely —
CLAUDE.md confirms the old auto-pause connection-pool trap died with Azure SQL.

## Secrets

`settings.py` resolves each secret from an env var first (underscored name), then
falls back to Key Vault by its hyphenated name, cached:

```python
secret("ANTHROPIC-API-KEY")   # -> $ANTHROPIC_API_KEY, else Key Vault
```

Deliberately **not** Terraform-managed Container Apps Key Vault references:
CLAUDE.md is explicit that Terraform must never own secret values, and a Key
Vault reference on a revision hard-fails if the secret is absent — which it
currently is, for all four. Env-first also makes `docker-compose` work off a
local `.env` with no Azure at all.

Four secrets to set by hand before the deploy works: `ANTHROPIC-API-KEY`,
`ALPACA-API-KEY`, `ALPACA-SECRET-KEY`, `RESEND-API-KEY`.

## Database schema and migrations

Numbered, idempotent SQL files in `sql/` (`001-schema.sql`, `002-seed-watchlist.sql`),
run by the **existing** `scripts/Invoke-DbSql.ps1`. That script currently takes a
single `-SqlFile`; extend it to accept several files or a directory and run them
in order under **one** firewall rule and **one** token — its own comments note
that firewall changes serialise on the server and a second one fails with
`ServerIsBusy`, so batching is what makes a migration run tolerable.

Chosen over Alembic because the repo already has this pattern, the schema will
change rarely, and it keeps Terraform, its state backend and git out of the path.
The honest cost: no down-migrations and no enforced ordering beyond the filename.
Every statement is written `CREATE ... IF NOT EXISTS` so re-running is safe, and a
`schema_migrations` table records what ran.

Tables per the brief — `companies`, `prices`, `news`, `news_analysis`,
`portfolio`, `positions`, `trades`, `ai_decisions`, `daily_performance`,
`benchmarks`, `agent_runs`, plus `daily_summaries` so the dashboard can render the
email body. Money is `NUMERIC(18,4)` throughout, never float; all timestamps
`timestamptz`.

`ai_decisions` is the audit-critical table and carries the full record the brief
asks for: `decided_at`, `ticker`, `action`, `confidence`, `reasoning`, `risks`,
`model`, `prompt_version`, `news_ids bigint[]`, `recommended_amount_gbp`,
`approved_amount_gbp`, plus `portfolio_state jsonb` and `risk_verdict jsonb` — so
every decision can be replayed later against exactly what the AI saw and what the
risk engine did to it.

## The agent job

`fetch state -> prices -> news -> cheap filter -> analysis -> risk engine -> paper
trade -> persist`, all inside one `agent_runs` row that records counts, token
usage and outcome.

Model cascade, one env var each:

- `claude-haiku-4-5` filters the news batch for relevance (cheap, high volume).
- `claude-opus-5` produces the investment assessment, with
  `thinking={"type": "adaptive"}` and `output_config={"effort": "high"}`.

Structured output via `client.messages.parse(..., output_format=Recommendation)`
reading `response.parsed_output` — a Pydantic model of action, ticker, confidence,
suggested amount, reasoning and risks. No `budget_tokens`, no assistant prefill,
no `temperature`: all three are rejected by these models.

> Worth flagging: the option you picked named `claude-sonnet-5`. I've defaulted
> the analysis stage to `claude-opus-5` because Anthropic's current guidance is
> to default there and at one run a day over ~10 tickers the difference is pennies.
> It's a single env var (`ANALYSIS_MODEL`) if you'd rather have Sonnet.

**The risk engine is the piece that matters most.** A pure function, no I/O, fully
unit-tested:

```python
def evaluate(rec: Recommendation, state: PortfolioState,
             limits: RiskLimits, trades_today: int) -> RiskVerdict
```

`RiskLimits` is config-driven: max position, max trade, max concentration, max
daily trades, max total exposure, an allowed-ticker allowlist, a minimum trade
size and a confidence floor. `RiskVerdict` returns the approved amount, every
reason applied, and the single binding constraint. Clamping order is explicit and
documented, and the brief's worked example is a test: recommend BUY £50 NVDA with
an £80 existing position against a £100 max position, approve £20.

Currency: Alpaca is USD, the experiment is GBP. A daily GBP/USD rate from
`frankfurter.app` (free, no key, ECB rates) is cached, stored on every trade and
every `daily_performance` row, so the £500 figure is always reconstructible.
Our Postgres tables are the source of truth for the portfolio; Alpaca is the
executor and the data feed. A `DRY_RUN` flag runs the whole loop and persists
decisions without submitting an order — the safe first deploy.

## The API and dashboard

FastAPI on port 8000: `/healthz` (no DB, for the probes), `/readyz` (DB ping),
then read-only `/api/overview`, `/performance`, `/holdings`, `/decisions`,
`/decisions/{id}`, `/news`, `/trades`, `/runs`, `/summaries/latest` — one per
dashboard view in the brief.

**Both apps are publicly reachable and unauthenticated today, and this plan does
not change that.** The data is paper-trading positions and AI reasoning: no PII,
no money, and no secret ever appears in a response. A shared-bearer dependency
goes in behind `API_REQUIRE_TOKEN` (off in dev) so the hook exists, but the real
fix is Container Apps EasyAuth with Entra, which `azurerm` does not expose and
would need `azapi` — out of scope here, and called out as the follow-up.

The dashboard is Vite + React + TS built to static files served by nginx on 8080.
It learns the API's address **at runtime**, not build time: nginx `envsubst`
renders `config.json` from `API_ORIGIN` on container start, the SPA fetches it on
boot, and FastAPI allows that origin via `CORSMiddleware`. One image, every
environment — no `VITE_API_URL` baked in, and no nginx `proxy_pass` resolver
puzzle. Recharts for the performance and benchmark charts; the `dataviz` skill
gets loaded before any chart code is written.

Benchmarks are honest proxies, since Alpaca only covers US-listed instruments:
`SPY` for the S&P 500, `VT` for a global index, `EWU` as a UK proxy (**not** the
FTSE 100 — labelled as a proxy in the UI), and cash at 5% computed arithmetically.

## Observability

Deliberately thin, because `daily_data_cap_in_gb = 0.1` on App Insights and
`daily_quota_gb = 0.15` on Log Analytics are genuinely tight and log ingestion is
this project's largest cost risk. Structured JSON to stdout (the Container Apps
environment already ships it to Log Analytics) plus one `agent_runs` row per
execution carrying counts, `response.usage` token figures and a computed USD cost.
No OpenTelemetry distro for now; the connection string is already injected, so
adding it later is a code change only.

## Tooling

- `uv` for Python (this container has no `pip` module), `ruff` for lint+format.
- Pre-commit additions: `ruff`, `prettier`, a local `tsc --noEmit` hook,
  `hadolint`. Today's local hooks are all `files: \.(tf|tfvars)$` and stay that way.
- **`check-added-large-files --maxkb=500` will reject `package-lock.json`** —
  raise the cap or exclude lockfiles, or the first dashboard commit is blocked.
- `.gitignore` is Terraform-only; add `__pycache__/`, `.venv/`, `.pytest_cache/`,
  `.ruff_cache/`, `node_modules/`, `dist/`, `*.egg-info/`, `.env`, `.env.*`.
- `.devcontainer/devcontainer.json`: **not edited here beyond the digest bump.**
  You own `docker-outside-of-docker` in the dev container image itself, so this
  repo's only change is moving the pinned digest to the version that carries it.
  `uv` gets installed by the Makefile bootstrap rather than baked in, unless your
  image ships it too.
- `Makefile`: new `build`, `push`, `migrate`, `deploy`, `run-agent`, `logs`, `up`
  targets. Widen the help `printf` from `%-10s` — the current width truncates the
  new names.

## Sequencing

**Step 0, immediately on approval: write this plan to `docs/deployment-plan.md`
in the repo and stop.** `/workspaces/market-agent` is a host bind mount and
survives a devcontainer rebuild; `/home/vscode` is the container's writable layer
and does not — so this plan, this conversation's history and any stored memories
are all lost the moment you rebuild. Committing the plan first is what makes the
rebuild safe. A fresh session started in this directory afterwards has `CLAUDE.md`
plus the committed plan and can resume at step 1.

1. Docker prerequisite (yours): publish a dev-containers image carrying
   `docker-outside-of-docker`, bump the digest in `.devcontainer/devcontainer.json`
   to it, rebuild. Then `.gitignore`, pre-commit, Makefile scaffolding.
2. `sql/001-schema.sql` and the `Invoke-DbSql.ps1` multi-file change; run it.
3. The Python package: settings, db, models, risk engine **plus its tests**.
4. Alpaca and Anthropic clients, then the agent job. Run locally in `DRY_RUN`.
5. FastAPI, then the dashboard, then `docker-compose` for the local loop.
6. The summary job and Resend.
7. Terraform image/port/args/probe changes; `make lint`; `checkov` skips.
8. Build, push, make packages public, `terraform apply`, verify.

## Prerequisites you have to do

These block a working deploy and I can't do them for you:

- Publish the dev-containers image with `docker-outside-of-docker` and bump the
  digest pin, then rebuild. Nothing in steps 4-8 can be built or run without it.
- `gh auth login` (or `docker login ghcr.io` with a `write:packages` PAT).
- Get an Anthropic key, an **Alpaca paper** key pair, and a Resend key, then
  `az keyvault secret set` all four into `kv-marketagent-dev`.
- After the first push, flip both ghcr packages to public — ghcr defaults new
  packages to private regardless of repo visibility, and Container Apps will fail
  the pull until you do.

## Verification

- `pytest apps/investagent` — risk engine first, including the brief's £50→£20 case.
- `docker compose up` — full stack against local Postgres, no Azure. Load the
  dashboard, confirm every panel renders from real API responses.
- `make migrate` then `psql` the server and check the tables and `schema_migrations`.
- `make deploy`, then `az containerapp job start` the agent and read its logs;
  confirm one `agent_runs` row, decisions in `ai_decisions`, and a trade in
  `trades`. Run it in `DRY_RUN` first.
- `curl https://ca-marketagent-dev-api.<env>.northeurope.azurecontainerapps.io/healthz`
  and load the dashboard FQDN. Expect a slow first response — `min_replicas = 0`
  means a cold start, which is the cost trade-off working as designed.
- `terraform plan` clean afterwards, and `make lint` green.

## Things that may bite, flagged now

- 0.25 vCPU / 0.5Gi is fine for FastAPI and for nginx serving static files. If
  the agent's LLM calls need more headroom, the step up is 0.5 vCPU / 1Gi —
  Container Apps requires memory at 2GiB per vCPU.
- The agent job's `replica_timeout_in_seconds = 1800` bounds the run. Adaptive
  thinking on Opus over many tickers can approach it, so the watchlist and the
  filtered-news count stay bounded, and raising the timeout is the lever.
- `schedule_trigger_config` forces replacement, so any cron change shows as
  destroy/create. Harmless — jobs hold no state.
