# Application

The Python package and the dashboard: money and the risk engine, the LLM
cascade, the agent job and the live APIs, the pots, the dashboard and its
passcode, the summary and weekly jobs, the API and the local stack. Moved out
of CLAUDE.md, which keeps the rules and links here.

## The application package

`apps/marketagent/` is one Python package, one image, five entrypoints
(`api`, `agent`, `sync`, `summary`, `weekly`). They share the risk engine, the database
layer, the broker client and the LLM client, so splitting them into four images
would mean four builds of near-identical layers. Deviates from the brief's suggested
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
  `Recommendation.suggested_amount_usd` arrives as a JSON number and is coerced
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
- `settings.secret("DEEPSEEK-API-KEY")` reads `$DEEPSEEK_API_KEY` first and
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

Migrated from Anthropic to **DeepSeek** — the git history of `llm/` before
that still shows the Anthropic shape (`messages.parse()`, `output_config`,
`thinking={"type": "adaptive"}`) if a comparison is ever useful. DeepSeek's
API is plain OpenAI-shaped REST, called through `fetch.py`'s own `httpx`
rather than a vendor SDK — there is no SDK dependency for it at all.

Two stages, two models, **one real complication**: DeepSeek's structured
output (`response_format: {"type": "json_object"}`) guarantees valid JSON,
**not conformance to a schema**. Anthropic's `messages.parse()` gave that for
free; here `llm/deepseek_provider.py`'s `_structured()` embeds the Pydantic
model's own `model_json_schema()` in the system prompt, parses the response
by hand with `model_validate_json()`, and retries once — feeding the
validation error back to the model — before raising `StructuredOutputError`.
That retry loop is the first place to look if a scheduled run starts failing
after this went in; it is new complexity the Anthropic integration never had.

- `deepseek-flash` (`FILTER_MODEL`) screens news for relevance, with
  `"thinking": {"type": "disabled"}` — reasoning is a request-level dial on
  DeepSeek, not a per-model fact, and the filter stage turns it off outright
  rather than asking for a lower effort.
- `deepseek-v4-pro` (`ANALYSIS_MODEL`) produces the assessment, with
  `"reasoning_effort": settings().analysis_effort` — one of `low`/`high`/`max`,
  DeepSeek's own scale, not Anthropic's five-step one.
- Neither stage uses an assistant prefill. DeepSeek supports one (Anthropic
  did not), but nothing here needs it.
- Pricing has a shape Anthropic's didn't: **separate cache-hit and cache-miss
  input rates**, not one input rate plus a multiplier, and **no separate
  charge for writing to the cache at all**. `llm/base.py`'s `Usage.cost_usd()`
  was redesigned around this — see its docstring — and it prices everything
  at DeepSeek's *peak* rate unconditionally rather than modelling DeepSeek's
  time-of-day/holiday pricing schedule, which happens to be exactly right for
  the two scheduled agent runs (both land inside the peak window) and a
  deliberate upper bound everywhere else.
- **The account's balance is read live**, not typed in. `GET
  /user/balance` (`llm/deepseek_provider.balance_usd()`) replaced the old
  hand-typed `ANTHROPIC-CREDIT-USD` secret — Anthropic published no balance
  endpoint at all, so that figure was a guess nothing checked. `jobs/summary.py`'s
  `_credit_usd()` catches a failed read and logs rather than raising, the same
  discipline `mailer.send` applies to email: a balance-API hiccup must not
  cost the day its summary.
- **A reasoning call needs a much longer read timeout than `fetch.py`'s
  default.** `TIMEOUT_SECONDS = 30.0` is sized for Alpaca/Frankfurter/
  Wikipedia GETs, not for `reasoning_effort="high"` — a real `static-100` run
  measured individual analysis calls taking 15-30s to answer even when
  nothing was wrong, which left that 30s default almost no margin, and a
  later run genuinely failed on exactly that (`ReadTimeout` after three
  retries). `post_json_retrying` therefore takes an overridable `timeout`,
  and `deepseek_provider.REASONING_TIMEOUT_SECONDS` (180s) is what the
  analysis/narrate/review calls use — the filter stage disables reasoning
  outright and keeps the ordinary 30s. If this starts firing again, the
  first thing to check is whether 180s has stopped being enough, not whether
  the retry logic is broken.

**The class docstring becomes the schema's `description`, and
`Field(description=...)` becomes each property's** — this predates the
DeepSeek migration and still holds, since the schema is still built from the
same Pydantic models via `model_json_schema()`. So those strings are prompt
text, not internal documentation — notes for a future maintainer go in a `#`
comment instead. The original `Recommendation` docstring explained Python
`Decimal` coercion and was being sent to the model verbatim.

For the same reason `Recommendation.suggested_amount_usd` is a **`float`**, the
only place in the system money is not a `Decimal`: declared as `Decimal` it
rendered as a three-branch `anyOf` — number, string with a Decimal regex, or
null — which is a worse thing to hand a model than a plain number. `money()`
converts via `str()` so a float's binary artefacts never reach a stored figure,
and it accepts `float` for that one caller only.

`tests/test_llm.py` is the only test file now: DeepSeek has no SDK to test
separately from the wire request/response, so the fake-client-double layer
and the real-SDK-through-a-mock-transport layer that the Anthropic version
needed (`test_llm.py` + `test_llm_wire.py`) collapsed into one — an
`httpx.Client` backed by `MockTransport`, the same pattern `fetch.py`'s other
callers are tested with.

### The agent job, and what the live APIs actually do

`jobs/agent.py` is the loop: state, prices, news, cheap filter, analysis, risk
engine, paper trade, persist — all inside one `agent_runs` row opened before
any work, so a crash leaves evidence. `llm` and `broker` are injectable, which
is the only way to exercise the loop without spending money and placing orders.

**The LLM cost, measured on a real run rather than estimated — under the
retired Anthropic cascade.** One complete run over the ten-name watchlist: 82
articles fetched, ~110 filter calls (one per article/ticker pair — an article
tagged with three watchlist names costs three), 46 relevant, 9 analyses,
**85,643 input and 11,658 output tokens, $0.18, and 4.1 minutes wall clock**.
At one run a day that was about **£4/month**, against the database's £13.

**These dollar figures do not carry over to DeepSeek**, whose per-token rates
are far lower but whose runs covered far more tickers. Measured on
2026-09-24, before the move to pots: static-100 made 47 analyses from 175
articles for **$0.39 in 36.5 minutes**, and dynamic-500 made 92 for **$0.73
in 65.5 minutes** — about **$0.008 and 38 seconds per analysis**, with 0
trades between them. Pricing those runs' output tokens alone at `v4-pro`'s
rate accounts for nearly all of each run's cost: the filter stage is a
rounding error, and the money is the analysis model's reasoning output
(about 2,000 tokens per analysis). About 90% of each run's wall clock was the
analysis loop, one call at a time.

Two things follow, and both shaped the pots below. Cost scales with the
number of tickers that have relevant news, not with the article count (which
`fetch_news` caps at 200). And every one of those 139 analyses was paid for
while both accounts sat at the 80% exposure ceiling with about $2 of buy
headroom against a $5 minimum, so no BUY could ever have been approved.

An earlier figure of $0.43 in this file was wrong: it came from a rehearsal with
a stubbed LLM whose token counts were invented, and it overstated the real cost
by more than double. Quote the measured figure, not that one.

Batching several articles into one filter call would cut the filter calls
sharply, but the filter is not where the money goes — it is a latency argument,
not a cost one.

### The pots

Three fixed pots, `tech`, `health` and `energy`, each about 50 S&P 500 names
from one sector (energy is Energy plus Utilities, since Energy alone has about
22). Each has its own Alpaca paper account and its own DeepSeek key, so
DeepSeek's usage page attributes cost to a pot. They replaced static-100 and
dynamic-500 in `sql/011-sector-pots.sql`, which marks the old accounts inactive
rather than deleting them: their history stays readable, but no job, API route
or dashboard tab iterates them.

- **The database is the list of pots.** `repository.active_portfolios()` is
  what the summary, the weekly review, the API's `Account` check and
  `/api/accounts` (the dashboard's tabs) all iterate. Terraform's `local.pots`
  is the other half, and the two must name the same pots.
- **Secret names are derived, not listed.** `accounts.secret_suffix("tech")` is
  `TECH`, so the pot reads `ALPACA-API-KEY-TECH`, `ALPACA-SECRET-KEY-TECH` and
  `DEEPSEEK-API-KEY-TECH`. The plain `DEEPSEEK-API-KEY` is the shared key the
  summary and weekly review use: they cover every pot in one call, so charging
  them to a pot would inflate that pot's bill. Every key draws on one DeepSeek
  account balance, so the runway is still computed once.
- **The lists are fixed.** The rebalance job and the Wikipedia S&P scraping are
  gone. A new pot is a migration row, a `local.pots` entry and its three
  secrets, with no code change.
- **Per-pot settings are env overrides in `local.pots`** (`ANALYSIS_EFFORT`,
  `RISK_*`, `MAX_RUN_COST_USD`). Every pot uses the same settings for now, so
  the comparison is between sectors. An override only lands when the job is
  created, because `env` is under `ignore_changes`; changing one later is
  `az containerapp job update --set-env-vars`. Each run records its effective
  settings in `agent_runs.config`, because `ai_decisions` stores the model but
  not the effort, and a decision has to be traceable to what produced it.
- **The agent only pays for analyses the risk engine could act on.**
  `risk.new_buy_headroom()` is the largest BUY `evaluate()` could approve for an
  unheld ticker (the BUY caps with nothing held). When it is below
  `min_trade_usd`, the run fetches news for and analyses only the pot's
  holdings, for a possible SELL; with the daily trade limit spent it analyses
  nothing. A test checks it agrees with `evaluate()` so the two cannot drift.
  Checked up front (orders submitted before the open do not fill mid-run, so
  the headroom cannot grow) **and again before each analysis**, because it can
  shrink: a BUY approved earlier in the run is an open order `risk_state()`
  reserves against exposure. Without the per-ticker check the 2026-10-25 week
  paid for tech and energy BUYs refused below the $5 floor by the exposure
  ceiling.
- **The model is shown the ceiling and its buying room.** `_prompt()` lists
  `max_total_exposure_pct` with the other limits and the largest BUY of that
  ticker the engine could permit now (`new_buy_headroom` with the ticker's own
  holding), and `ANALYSIS_SYSTEM`'s point 5 says to HOLD when that is under the
  minimum and never to ask for more. Both are in `prompt_context`
  (`buy_headroom_usd`, `trades_today`). `PROMPT_VERSION` went to `v7` for it.
- **The pots run concurrently**, as separate jobs at the same cron time. A pot's
  run length is its analyses in series, so a 50-ticker pot is roughly 10-18
  minutes on a normal day. Expect about $0.10-0.19 a day per pot while it is
  buying and a few cents once it is full.

**Cost must be accumulated per call, never derived from token totals.** The two
stages use different models at different rates (under Anthropic this was
measured at 30% high when the filter's Haiku tokens were priced as Sonnet —
$0.566 against a true $0.434), so pricing a mixed token total at either
model's rate is simply wrong; DeepSeek's cascade carries the same risk, now
also cache-hit-vs-miss-aware (see `Usage.cost_usd()` in `llm/base.py`). The
token columns still hold the mixed totals, which is fine because they are
counts.

**The model is shown its own recent decisions on the ticker.** Five of them,
most recent first, with the engine's verdict and the resulting order's status —
`repository.recent_decisions`, covered by the `ai_decisions_ticker_idx` that
already existed. Without it nothing in the prompt lets the model know it has
argued the same BUY four mornings running, or that every one was clamped to the
same cap. The prompt says explicitly what the history does *not* mean: a
refusal is a statement about the limits rather than about the reasoning, and an
order's status is not its outcome. It is decision memory, not a score.

The consequence is `prompt_context`, added by `006-decision-context.sql`. The
history is an input to the decision that `portfolio_state` does not carry, so
without a column for it the replayability claim on an `ai_decisions` row
quietly stops being true. Structured rather than the rendered prompt text, for
the reason the weekly review's proposals are: "has it asked for the same thing
three days running?" stays a query. **Anything added to `_prompt()` needs the
same treatment** — a new input with nowhere to be stored breaks the row's claim
to be a complete record of what was asked.

**One run's model spend is capped by `MAX_RUN_COST_USD`**, defaulting to $1.00
— set against Anthropic's measured $0.18–0.19 and not yet revisited for
DeepSeek's materially lower rates, so it is a looser ceiling than it looks
until it is. It is checked as each call's cost lands rather
than once per stage, because the stage that can run away is the filter at one
call per article/ticker pair — exactly the one a per-stage check would let
finish first. Crossing it raises `BudgetExceeded`, which the existing handler
closes the `agent_runs` row with, so the failure reads like any other. `gt=0`
on the setting rather than "0 disables": a guard that switches off at the value
which reads like "spend nothing" is the wrong footgun to leave lying around.
The cost ceiling is not wired through Terraform — its default applies to the
deployed job and an override is an env var on the container. `DRY_RUN` is wired
through Terraform's `agent_dry_run`, which defaults to false for paper trading.

**"Submitted, no fill" is the normal outcome of a scheduled run.** The agent
runs at 10:30 UTC and the US market opens at 13:30/14:30, so a market order
sits `accepted` for three or four hours. Consequences: `trades.quantity` had to become
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

### The dashboard

`apps/dashboard/` is Vite + React + TypeScript built to static files and served
by **unprivileged nginx on 8080** — 8080 rather than 80 because a non-root
process cannot bind below 1024.

- **It learns the API's address at runtime.** The entrypoint renders
  `config.json` from `$API_ORIGIN` at container start and the SPA fetches it on
  boot, so one image serves every environment. A build-time `VITE_API_URL`
  would produce an image that is only correct where it was built.
- **The rendered `config.json` goes to `/tmp`, not the document root.** The
  unprivileged image runs as uid 101 and `/usr/share/nginx/html` is root-owned,
  so writing there fails at start-up; nginx aliases the one path, which keeps
  the served root read-only anyway. It is also `no-store`: a cached copy from a
  previous revision points at the wrong API and reads as a CORS bug.
- **The chart palette is validated, not chosen.** The five series use the
  reference categorical palette in its documented slot order — that order *is*
  the colourblind-safety mechanism, so reordering or cycling it breaks the
  guarantee. Colour follows the entity rather than its rank, so hiding a series
  never repaints the others. Light mode puts three slots under 3:1 against the
  surface, which obliges relief: the **table view beside the chart is a required
  accessibility channel**, not a convenience. One y-axis, because every series
  is the value of the same notional £500.
- A benchmark with no data is omitted rather than drawn flat at the notional,
  matching the summary job.
- **The dashboard is gated behind a passcode, held in `DASHBOARD-PASSCODE`.**
  This sits beside, not instead of, the "both apps are public and
  unauthenticated" design further up: the data is still non-sensitive, so the
  goal is keeping casual bots and scanners off a public URL, not defending
  against a determined attacker — and it needed to work for people with no
  Microsoft account, which ruled out Entra/EasyAuth despite that being the
  properly "correct" fix.
  - **`docker-entrypoint.sh` fetches it from Key Vault itself**, with the
    container's managed identity (`AZURE_CLIENT_ID`/`KEY_VAULT_URI`, an MSI
    token exchange against `IDENTITY_ENDPOINT`/`IDENTITY_HEADER`, then a plain
    REST call for the secret) — the same way every other secret in this repo
    is read, just from shell rather than from `settings.secret()`. This
    replaced Container Apps' native Key Vault secret reference
    (`key_vault_secret_id` on a `secret` block), which resolves a secret's
    value once, at revision creation, and caches it for every replica and
    every restart of that revision. Rotating the passcode then needed a
    brand-new revision to actually take effect — a plain
    `az containerapp revision restart` silently kept serving the value from
    whenever the revision was first created, which cost a working session to
    diagnose, since the entrypoint's own "passcode gate enabled" log line gave
    no hint it was using a stale value. Fetching it ourselves means a plain
    restart is enough, and it no longer has to exist in Key Vault before the
    `terraform apply` that first deploys the dashboard, either. `curl` and
    `jq` are in the image for exactly this.
  - **It replaced a `DASHBOARD-USERNAME`/`DASHBOARD-PASSWORD` pair**, because
    the browser's native Basic Auth dialog always asks for a username and
    there was only ever one account. Delete those two secrets once a restart
    carrying the passcode is up.
  - **`Set-KeyVaultSecrets.ps1` generates it** — press Enter at its prompt —
    and prints it once. It is the only secret in this repo that is generated
    rather than typed. Terraform still generates **nothing**: the rule that it
    creates no `azurerm_key_vault_secret` is what keeps every secret value out
    of Terraform state, and a `random_password` would have put the passcode
    straight into it.
  - **The alphabet excludes `;` `,` `"` `\` and whitespace**, and that is
    load-bearing rather than cosmetic. The passcode travels as a cookie value,
    where `;` and `,` terminate it, and nginx compares it inside a quoted
    string in a generated config, where `"` and `\` terminate that. The
    entrypoint **refuses to start** on any of them rather than rendering a
    config that compares against a truncated passcode; the script refuses to
    store one for the same reason. It also drops `0`/`O` and `1`/`I`/`L`,
    which are what get mistyped reading a code off another screen.
  - **Both also refuse a non-ASCII character** — a curly quote or a pound sign
    pasted from somewhere that "smartened" it, say. That one is not cosmetic
    either: `login.html`'s `encodeURIComponent` percent-encodes it into the
    cookie, while the entrypoint substitutes it into the nginx config raw, so
    the two can never compare equal — retyping it, however carefully, cannot
    fix that. A hand-set `DASHBOARD-PASSCODE` containing one passed silently
    until this was added, and cost a working session to diagnose because
    every other symptom looked like a stale-secret problem instead.
  - **nginx compares the cookie verbatim — there is no signed session**, which
    is where this differs from `gym-log`'s passcode gate. gym-log can HMAC an
    issue time into a cookie because it is a Python app; this image is static
    files and nginx, with nothing to do the signing. So the cookie *is* the
    passcode, which is the same exposure Basic Auth had — that credential also
    rode on every single request, just base64'd rather than in a cookie.
  - **A 401 with `error_page 401 /login.html`, not a redirect to a login
    route.** The request keeps the URL it asked for, so a deep link to
    `/holdings` survives the login and lands on `/holdings` after the reload;
    a redirect would have to carry the original path and put it back.
    `login.html` is `internal`, so it is only ever reachable as that error
    body, and it is outside the auth check — a login page behind the gate it
    exists to open is a loop. It carries no secret: what the person types goes
    into a cookie and nginx does the comparing.
  - Absent means open, which is what keeps `docker compose up` serving the
    dashboard with no credential configured. To exercise the gate locally:
    `DASHBOARD_PASSCODE=LOCAL-TEST-CODE make demo`.
  - The check is `include`d explicitly into each protected `location` (`/`,
    `/assets/`, `= /config.json`) rather than placed once at `server` level, to
    avoid relying on how `if` inherits into sibling locations; `= /healthz`
    deliberately has no such include, for the same reason the API's own health
    endpoints sit outside its bearer gate — a Container Apps liveness probe
    cannot present a credential.
- `shellcheck`'s SC2016 fires on `envsubst '${API_ORIGIN}'`, where the single
  quotes are the point — expanding them would substitute the value into the
  variable *list* and leave the template untouched. Suppressed inline with that
  reason.

### The summary job

`jobs/summary.py` runs at 21:00 UTC and does the thing the agent could not:
the agent submits at 10:30 and the US market opens at 14:30, so **this is where
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

- **The email reports model spend, and the balance it's checked against is
  read live.** Unlike Anthropic — which publishes usage and cost reports
  (`/v1/organizations/usage_report/messages`, `/v1/organizations/cost_report`)
  but nothing that returns a remaining prepaid credit balance, and both of
  those need an Admin API key individual accounts cannot hold at all —
  DeepSeek exposes `GET /user/balance` directly, wrapped as
  `llm/deepseek_provider.balance_usd()`. Spend itself is still built from our
  own ledger: `repository.spend()` sums `agent_runs`, `daily_summaries` and
  `weekly_reviews`, and the email shows today, the last seven days with a
  daily rate, and the total. `007-job-costs.sql` exists because the other two
  jobs never recorded their own call — the weekly review logged the figure and
  discarded it, the summary never looked — so a total from `agent_runs` alone
  was low by a call a day and a call a week, and the gap only grows.
- **The runway is computed from a live-read balance, not a number a human
  supplies.** `jobs/summary.py`'s `_credit_usd()` calls `balance_usd()`;
  absent means report spend and no runway, the same shape the old opt-in
  `ANTHROPIC-CREDIT-USD` secret had, but now driven by whether the read
  succeeded rather than whether anyone configured it — there is nothing left
  to configure. A failed read is caught and logged rather than fatal, the same
  discipline `mailer.send` applies to email: a balance-API hiccup must not
  cost the day its summary.
- **The spend figures cannot include the call that writes the email**, because
  the table is built before `narrate()` runs. The section says so in the text
  rather than leaving it to be inferred, alongside the fact that the balance is
  DeepSeek's own reported figure rather than anything typed by hand. Both are
  there so the model cannot describe the figure as complete — the same
  discipline as the trades table. The daily rate is over the last seven days,
  not all time, because the question behind it is "how long at the current
  rate" and the first week of
  manual runs answers a different one.
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
  from it.
- **The recipient lives in Key Vault, not in `common_env`.** This repository is
  public and so are `terraform/environments/*.tfvars`, so an address in either
  is committed permanently. It resolves through
  `optional_secret("SUMMARY-EMAIL-TO")`, which reads the environment first and
  then the vault — so a local `.env` still works, and changing the recipient in
  a deployed environment is `az keyvault secret set` plus the next run, with no
  redeploy. Absent means store the summary and send nothing, so sending is
  opt-in: a job that emails on every development run is worse than one switched
  on deliberately.
- **A stored recipient must be plain ASCII.** Resend rejects a `to` containing
  anything else with a 422 (`The email address contains non-ASCII characters`),
  and the 2026-09-03 summary was lost to exactly that: the value in Key Vault
  had been pasted from somewhere that autocorrects quotes and was stored as
  `‘you@example.com’`. Curly quotes are invisible in `az keyvault secret
  show` output, so read the codepoints (`| cat -A`) rather than the string when
  a send fails on the address. `Set-KeyVaultSecrets.ps1` now warns on it — its
  advisory pattern is printable-ASCII either side of the `@`, because the
  obvious `[^@\s]` accepts U+2018 happily.
- **`optional_secret` treats absence and failure differently.** A missing
  variable, no vault, or a secret not in the vault all return `None`; an
  authentication or network error propagates, because "the credential is
  broken" must never look like "no email configured". `secret()` raising stays
  right for an API key the job cannot run without.
- Reconciliation commits per trade: one order the broker cannot answer for must
  not roll back the fills already applied.

### The weekly review

`jobs/weekly.py` runs at 22:00 UTC on Sunday and answers the question a single
day cannot: is the *machinery* working, and which knob is worth turning. It
reads a week of the tables the agent and the summary already wrote, hands the
model those figures, and stores an assessment plus structured proposals.

- **It reads only** — no market data, no news, no broker, one Sonnet call. The
  week is already written down, and a review that re-fetched could report
  figures the stored series disagrees with. It also means running it repeatedly
  costs one model call and changes nothing.
- **22:00, an hour after the summary**, because the week's closing valuation
  *is* that evening's `daily_performance` row. Moving
  `weekly_review_cron_expression` before `daily_summary_cron_expression` would
  silently review a week ending a day early.
- **A week with no `agent_runs` rows is not reviewed at all**, and `run()`
  returns `None`. An LLM call to say nothing happened is worth neither the
  money nor a stored row that reviews nothing.
- **The binding-constraint histogram is the point of the whole job.**
  `ai_decisions.risk_verdict->>'binding_constraint'` grouped by outcome answers
  "which limit is actually shaping this experiment", which is unanswerable from
  any single day. Refusals and approvals are split into two tables: a cap
  binding an approval and a gate refusing one are opposite facts, and one table
  reads as a single ranking of "constraints that fired".
- **The limits table carries the env var name beside each value**
  (`RISK_MAX_DAILY_TRADES` and friends). A proposal has to name the knob to be
  actionable and the model has no other way to learn what they are called. The
  values come from `risklimits.limits()` — the same call the engine makes — not
  a second read of the same environment.
- **Proposals are advisory and structured.** `ProposedChange` has a closed
  `area` literal for the same reason `Constraint` does:
  `weekly_reviews.recommendations` is queryable, so "has it asked for the same change three
  weeks running?" is a query rather than a re-read of the prose. Nothing reads
  them back and applies them — a risk limit changes when a human edits
  `risklimits.py`, which is the entire point of the limits sitting outside the
  model's reach. The system prompt says so, and there is a test that it does.
- **A `below_min_trade_usd` refusal is split by what made it too small**, and
  **decisions are split by `prompt_version`.** Both exist because the
  2026-10-12 review re-proposed a prompt change already shipped in v6: it
  could see neither that the prompt had changed nor that the refusals came
  from a cap clamping a normal-sized BUY under the $5 floor rather than from
  the model asking for too little. The cause is the reason with the smallest
  `cap_usd`, ties to the earlier one — the same pick `evaluate()` makes.
  `PROMPT_VERSION` is shared by every prompt, so a new version says *some*
  prompt changed, not which.
- **The previous review's proposals go into the prompt**, so the review
  compounds instead of restarting every Sunday. The model is told the list
  cannot tell it whether any were acted on — a rejected proposal and one nobody
  read look identical — and that the limits table is the current state to
  compare against.
- `assessment` is stored in its own column as well as inside `body_markdown`.
  The dashboard renders that column as **text**: `body_html` is
  Markdown-rendered *model output* and Python-Markdown passes raw HTML straight
  through, so putting it in the DOM would hand the model an XSS vector for
  nothing. `api.ts`'s `plain()` strips the inline Markdown markers instead.
- The recipient is the **same** `SUMMARY-EMAIL-TO` secret as the daily email —
  one reader, one address, and a second secret to forget to set would be worse.
- `--as-of YYYY-MM-DD` reviews a past week. Cheap to offer, because the job
  reads only stored rows, and the first run wants last week rather than today.

#### The data integrity checks

`repository.week_integrity` runs six deterministic checks and the review
renders them as a pass/fail table, with the count of failures appended to the
**subject line**. Every one exists because the failure it catches is *silent*.
The week of 2026-09-14 lost the whole of the 09-17 `daily_performance` row and
the whole of the 09-18 agent run, and nothing said so — the chart drew a
straight line across the gap and the next email reported a total that had
moved for no stated reason. That is the class of fault these catch.

- `missing_valuations`, `missing_runs`, `failed_runs` — a day with no
  valuation, a day with no agent run, and a run that failed or was abandoned
  past `JOB_TIMEOUT_SECONDS`. The agent cron is daily including weekends, so
  there is no trading calendar to reason about.
- `stale_prices` — a holding whose newest bar is over `STALE_PRICE_DAYS` (4)
  old. This is the one that silently moves the reported total: `build_state`
  **drops** an unpriced holding from the valuation rather than carrying it
  forward, so the total understates and then rebounds when the price returns,
  with no trade behind either move.
- `price_spike` — a held ticker moving more than `SPIKE_PCT` (25%) in a day.
  Bars are fetched with `adjustment=all`, which restates the close across a
  split while `positions.quantity` stays exactly as it was, so a split moves
  the reported value with no trade behind it. 25% is deliberately well clear
  of a real move — AMD did +9.2% in a day in September 2026 and must not fire
  it. A check that cries wolf is ignored within a month.
- `cash_drift` — `cash_gbp` reproduced from the trade log alone. This is what
  would catch an `apply_fill` regression: a fill applied twice, or one that
  moved the position without the cash.

Three things not to undo:

- **The checks are computed and rendered by us, never summarised by the
  model** — the same discipline as the daily email's subject line. A failure
  the model could smooth over in prose is not a check. `REVIEW_SYSTEM`'s point
  5 tells it to lead with a failure and not to explain a move a broken record
  may have invented; `PROMPT_VERSION` went to `v4` for that.
- **Every check reports, pass or fail.** A section that appears only on a bad
  week is one nobody learns to read, and its absence then looks identical to
  the job having skipped it.
- **`week_integrity` is called after the no-runs short-circuit**, not before.
  A week with no runs at all is not reviewed, so there is nowhere to report a
  check — note the consequence, which is that the loudest possible failure
  (the agent never ran once all week) leaves this job silent, and the daily
  email is what covers it one day at a time.

It assumes the window has closed, which the 22:00-after-21:00 ordering
guarantees. A manual run earlier in the day reports today as a missing
valuation, which is true rather than spurious — that week has not finished.

### The API and the local stack

`api/` is **read-only**. Every mutation belongs to the agent, summary and weekly
jobs,
so this process cannot place a trade or alter a decision however it is called —
which is most of why it is comfortable being publicly reachable. `/api/*` is
guarded by a shared bearer (`API_REQUIRE_TOKEN`, off by default in application
code but on in Terraform for this deployment — see below); the real fix is
Container Apps EasyAuth with Entra, which `azurerm` does not expose.

- **`API_REQUIRE_TOKEN` is set in Terraform, not left at its off-by-default.**
  This closes the gap the dashboard's own Basic Auth cannot: the dashboard is
  just a browser-side client of this app's public FQDN, so gating the
  dashboard alone would still leave every `/api/*` route reachable directly by
  anyone who finds that FQDN. The token itself, `API-BEARER-TOKEN`, is one
  secret shared with the dashboard — read lazily via `secret()` here, the same
  as every other application secret, but also wired into the dashboard
  container as a Key Vault secret reference and rendered into its
  `config.json` (`apiToken`) so the browser can forward it as
  `Authorization: Bearer <token>`. Anyone who has already cleared the
  dashboard's Basic Auth can read that token out of `config.json` and call the
  API directly forever afterwards — an accepted boundary given the data is
  non-sensitive, not a gap. On an already-existing API app this needs pushing
  by hand once with `az containerapp update --set-env-vars
  API_REQUIRE_TOKEN=true`: the whole `env` list on that resource is under
  `lifecycle { ignore_changes }` for `IMAGE_TAG`'s sake, so a plain
  `terraform apply` reports no diff (see `main.container-apps.tf`).
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
image failed with a bare `No module named 'marketagent'` from a venv that looked
complete. `--no-editable` on both syncs is the fix.

The image runs as **uid 10001, numerically** — `USER marketagent` trips
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

**`actionlint` runs shellcheck over every `run:` block — but only if the
shellcheck binary is on `PATH`, and silently skips it otherwise.** No warning
and no skipped-hook notice, so an environment without it lints workflow shell
*not at all*: `make lint` passes and CI fails on the same commit. It cost a
round trip on a comment line that happened to wrap onto the word `shellcheck`,
because a comment opening `# shellcheck` is a *directive* rather than prose,
and the parse error (`SC1072: Expected '=' after directive key`) is reported
against the whole `run:` block rather than the line.

The dev container carries shellcheck as of
[dev-containers#67](https://github.com/jay-withers/dev-containers/pull/67), so
this is handled here — but **rebuild the container if `command -v shellcheck`
comes up empty**, and expect the trap anywhere else. Note that *having a
shellcheck pre-commit hook does not help*: this repo has `shellcheck-py`, which
installs into its own isolated hook environment and is not on `PATH` for the
actionlint hook, and workflow shell was going unlinted regardless.

**`ruff-format`'s upstream hook includes `markdown` in its `types_or`**, so it
reformats Python code blocks inside `.md` files — it rewrote the aligned
comments in `docs/deployment-plan.md` on its first run. The hook is pinned to
`types_or: [python, pyi]` in `.pre-commit-config.yaml` for that reason; don't
drop the override. Ruff's own config lives in `apps/marketagent/pyproject.toml`
and is found by walking up from each file, so nothing points at it from the
pre-commit config.
