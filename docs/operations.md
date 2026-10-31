# Operations

Commands, deploys, testing, tooling, CI, Renovate and repo settings: how the
repo is built, checked and shipped. Moved out of CLAUDE.md, which keeps the
rules and links here.

## Dev container

The repo is built around the dev container at `.devcontainer/devcontainer.json`,
which uses the image `ghcr.io/jay-withers/dev-containers/terraform`. It provides
Terraform, TFLint, terraform-docs, and Checkov, and runs `make install` on
creation. Prefer working inside the container so tool versions match CI.

## Commands

`make` with no target prints the self-documenting help (the default goal).

```bash
make install           # pre-commit hooks and Python dependencies
make test              # the Python test suite (pytest)
make lint              # all pre-commit hooks against every file
make secrets           # prompt for the Key Vault values and store them
make sql               # every file in sql/, in filename order, against the database
make up / down / logs  # the local docker compose stack
make demo              # the local stack seeded with fake data, for a dashboard preview
make run-agent         # one agent run against the local stack
make run-weekly        # one weekly review against the local stack
make build             # both images for linux/amd64, tagged with the git SHA
make push              # both images to ghcr.io (needs write:packages)
make deploy            # az cli: roll that image tag onto every app and job
make logs-azure        # tail a pot's deployed agent job logs (PORTFOLIO=tech)
make fmt               # terraform fmt -recursive
make validate          # terraform init + validate (no Azure credentials)
make plan              # terraform init + plan (set ENV=dev|stg|prd, default dev)
make apply             # terraform init + apply (set ENV=dev|stg|prd, default dev)
```

**A local `make plan`/`make apply` needs the operator's values exported.**
`key_vault_administrator_object_ids`, `alert_email_address` and
`postgres_entra_administrator` aren't committed: CI reads them from the
`TF_VAR_*` variables github-repos sets on the `dev` environment. Copy them from
`gh variable list --env dev`, lower-casing the name after `TF_VAR_`
(`export TF_VAR_alert_email_address=…`).

**`make sql` is not part of `make deploy`, and the schema is not versioned with
the image.** A migration the code depends on has to be applied first, or the
deployed workload fails against a database that predates it — the agent job
queries `companies.is_benchmark`, so it would have failed outright on a
database holding only `001` through `003`. This has already been forgotten once.
The weekly review job is the same shape of dependency: it needs `005` for
`weekly_reviews`, and its first Sunday run against a database without it fails
on a missing relation. `006-decision-context.sql` is the same again — the agent
writes `ai_decisions.prompt_context` on every decision, so a deploy of that code
against a database without it fails on the first analysis, not at start-up. So
is `007-job-costs.sql`, which the summary and weekly jobs write to on every run.
And `011-sector-pots.sql`, the sharpest of them: the API, the dashboard and
every job read `portfolio.is_active`, and `agent_runs.config` is written at the
start of every run, so a deploy ahead of it breaks everything at once.

**`make deploy` is `az cli`, not `terraform apply`.** The image and the
`IMAGE_TAG` env var on the API app and all three jobs carry
`lifecycle { ignore_changes = [...] }` in `main.container-apps.tf` and
`main.container-apps-jobs.tf`, specifically so a deploy can move independently
of a plan/apply cycle — no state lock, no plan of unrelated infra drift, no
risk of a stale local `.tfvars` rolling the image backwards. `marketagent_image_tag`
/ `dashboard_image_tag` (the Terraform variables) only seed the *first*
revision on a brand-new environment; every deploy after that is `make deploy`,
which resolves the app/job names and resource group from `terraform output`
(`api_app_name`, `dashboard_app_name`, `summary_job_name`,
`weekly_review_job_name`, `resource_group_name`, and the `agent_job_names` and
`sync_job_names` maps, read with `jq`) and calls
`az containerapp update` / `az containerapp job update --image ... --set-env-vars
IMAGE_TAG=...` against each. `--set-env-vars` adds/updates only the name given
and leaves every other env var alone, which is what lets `IMAGE_TAG` move
without disturbing `KEY_VAULT_URI`, `POSTGRES_HOST` and the rest of
`common_env`.

The trade-off of ignoring the whole `env` list rather than indexing into
`IMAGE_TAG`'s one entry: a future change to any of `common_env`'s *other*
values needs a `make deploy` to actually land on a running revision, not just
`terraform apply` — a `terraform plan` after such a change will report no diff
on the container even though the value it computes has moved. Indexing into
the map-driven `dynamic "env"` block by position was the alternative and was
rejected — that index would silently shift if `common_env` ever gained or lost
a key, ignoring the wrong entry with no error.

**There is one image tag variable per image**, `marketagent_image_tag` and
`dashboard_image_tag`, not one shared between them, and the same split carries
through to `make deploy`'s `MARKETAGENT_IMAGE_TAG`/`DASHBOARD_IMAGE_TAG`. Both
images are built and pushed under the same short SHA, so in the normal case the
two hold the same value — `make deploy` sets both from one `IMAGE_TAG`, so the
tag built is still the tag deployed and the two cannot drift. The split exists
for the abnormal case: rolling the dashboard back to yesterday's release while
the API stays on today's, without rebuilding either.
`make deploy MARKETAGENT_IMAGE_TAG=v0.4.0 DASHBOARD_IMAGE_TAG=v0.3.3` is that.
`make deploy` guards against `latest`/`main`/`unset` the same way the Terraform
variable validation does — the Terraform validation only fires on
`terraform apply` now, which no longer runs on the day-to-day deploy path.

**`make deploy` on its own — no tag argument — is a hard error, unlike
`build`/`push`.** Those two default `IMAGE_TAG` to the local git SHA, which is
exactly what you want when iterating: build, push, and the tag you just built
is right there. Deploying is different — a bare `make deploy` would silently
roll Container Apps onto whatever commit happens to be checked out, which may
never have been pushed to ghcr.io at all, or may be stale relative to what CI
last published. `IMAGE_TAG_EXPLICIT`/`MARKETAGENT_TAG_EXPLICIT`/
`DASHBOARD_TAG_EXPLICIT` check `$(origin ...)` for each — `file` means the `?=`
default fired rather than the caller — and `deploy` refuses to run unless
`IMAGE_TAG` was passed, or both of the per-image variables were (passing only
one still errors, since the other would silently fall back to the git-SHA
default). `build`/`push` are untouched: the default there is the point.

Both Terraform variables default to a **published release tag**, not a
sentinel, so a bare `terraform apply` against a brand-new environment deploys
something real. Mind the `v`: cd-publish pushes each image as `vX.Y.Z` *and*
the commit's short SHA, so `0.4.0` is not a tag that exists — a default without
the prefix fails at revision start-up on an image pull, long after plan and
apply both reported success.

`common_env`'s `IMAGE_TAG` carries the **marketagent** tag specifically, since
that is what the agent records on its `agent_runs` row and it has to name the
image the code writing the row is running. The dashboard never reads it, and
`make deploy` only sets it on the API app and the three jobs.

### Testing the SQL

`repository.py` and `queries.py` are hand-written SQL, and until
`tests/test_repository.py` and `tests/test_queries.py` existed nothing executed
any of it: the first run of a statement was the deployed job at 06:00 UTC.
Both modules sat near 30%; they are now at 97% and 98%.

- **A real Postgres, not a mocked connection.** The failures worth catching are
  the ones only the server can raise — a column that does not exist, a foreign
  key to a company nobody seeded, a CHECK constraint refusing a status string,
  an `ON CONFLICT` naming the wrong index. A psycopg double accepts all four.
  Writing these caught exactly that class of thing in the *tests* themselves:
  `agent_runs.status` rejects `success` (the code correctly writes `succeeded`).
- **`POSTGRES_TEST_DSN` gates them, and its absence skips rather than fails.**
  `make test` therefore still works on a clone with no Docker — 299 pass and 68
  skip — and `make test-db` runs the lot. Deliberately not `DATABASE_URL`: a
  developer with that pointing somewhere real would have the fixtures create
  and drop databases on it, which they do.
- **The fixtures apply `sql/0*.sql` to a scratch database** created and dropped
  per session, so a run never touches rows a developer was looking at. Lines
  starting with `\` are stripped first — each migration ends with `\echo`
  progress lines that the server cannot parse — which is what keeps the harness
  free of a `psql` on PATH, worth having since psql has gone missing across a
  dev container rebuild before.
- **Isolation is per-test rollback on one session connection**, not a rebuilt
  schema: no function in `repository.py` commits, since the caller owns the
  transaction — that is what lets the agent put a decision and its trade in one
  — so discarding the transaction discards the test. The whole database suite
  runs in well under a second.
- In CI the server comes from the shared workflow's `postgres` input, which
  starts one with `docker run` and exports `POSTGRES_TEST_DSN`. It is `docker
  run` rather than a `services:` block because a service cannot be made
  conditional — an empty image is a workflow error — so a `services:` block
  would impose Postgres on every repo calling that workflow.

`jobs/agent.py` is the largest remaining gap at 32%: the loop itself, which
needs the same database plus injected `llm` and `broker`. The pieces are now in
place for it.

`make test` runs the Python suite. There are no *Terraform* tests — those were
removed at the user's request — and `make validate` is the credential-free
Terraform check.

`make install` is expected to be re-run after a dev container rebuild, not only
after a clone: `uv` installs into `~/.local/bin`, which is the container's
writable layer and does not survive one. `az login` and `gh auth login` go the
same way, and so does anything `apt-get install`ed by hand — `psql` was missing
after the last rebuild despite the deployment plan recording it as present.

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

Workflows are prefixed `ci-` (pull-request checks) or `cd-` (post-merge
delivery). **Every one of them is a thin caller of a reusable workflow in
[`jay-withers/workflows`](https://github.com/jay-withers/workflows)** — pinned
by commit SHA with the tag as a comment, and holding only what is specific to
this repo. A change to how a job *works* belongs in that repo so every
consuming repo picks it up; only what is specific to market-agent stays here.

Because they are reusable-workflow calls, the status check contexts are
namespaced `<caller job id> / <reusable job name>` rather than the bare job id
— read them off `gh pr checks` rather than inferring them, and see
"GitHub repo settings" for which ones branch protection requires.

- **ci-pre-commit**: runs all linters on PRs to `main` via the `pre-commit` job,
  which calls the reusable workflow
  `jay-withers/workflows/.github/workflows/pre-commit.yml` (pinned by
  commit SHA, with the tag as a comment). Because it's a reusable-workflow call,
  the status check context it reports is `pre-commit / Pre-commit`
  (`<caller job id> / <reusable job name>`), not the bare `pre-commit` job id.
- **ci-terraform**: `validate` (`init -backend=false` + `validate`, no
  credentials) from the shared `terraform.yml`, reporting `terraform / Terraform`,
  and a dev `plan` from the shared `terraform-deploy.yml`, which comments the
  plan on the PR. Same shape as gym-log and the other app repos.

  - **The plan runs in the `dev` GitHub environment**, as
    `uai-market-agent-deploy-dev` (github-repos' `state_consumers`), which holds
    Contributor and conditioned RBAC Administrator on `rg-marketagent-dev`,
    Reader on the subscription and join on `cae-platform-dev`. The environment's `AZURE_*` variables are invisible to
    the calling job, so nothing here gates on them.
  - **Only dev is planned.** Stg and prd remain in the configuration (and keep
    their tfvars and backends) but have no identity and are never applied.
  - **`plan` reuses the shared workflow's path filter** via its `changed`
    output, so it starts after `validate`, and needs its own always-reporting
    gate, `terraform-plan`, because `terraform / Terraform` only covers what
    runs inside the shared workflow. Skipped counts as success.
- **cd-terraform**: plans dev on every merge touching Terraform and **applies
  only when run by hand** (Actions → Run workflow), applying exactly the plan
  it prints. A plan with changes opens a `terraform-pending` issue; a failed
  apply opens `deploy-failed`. The identity has Contributor and conditioned
  RBAC Administrator on `rg-marketagent-dev` (github-repos creates the group;
  `data.azurerm_resource_group.this` reads it), so role assignments inside the
  group apply here too. It can't write to `cae-platform-dev` beyond joining
  it, so an apply that (re)creates the dashboard's managed certificate still
  runs locally (`make apply ENV=dev`). Images deploy through cd-publish, not
  here.
- **ci-python**: runs `pytest` on every PR, by calling the reusable
  `jay-withers/workflows/.github/workflows/python.yml` (pinned by commit SHA
  with the tag as a comment, like `ci-pre-commit`). Nothing in CI ran the suite
  before it — `ci-pre-commit` lints with ruff and `ci-container-build` proves
  the images compile, but neither executes a test, so the risk engine was
  guarded only by whoever remembered `make test`. The sharp edge was Renovate:
  `autoApprove` means a dependency bump reaches `main` with no human reading
  it, so a `psycopg` or `pydantic` release that broke the engine or the
  request shapes would have merged green. Being a reusable-workflow
  call, its status check context is **`test / Test`**, not the bare `test` job
  id — the same namespacing `pre-commit / Pre-commit` has.

  Three inputs, and `extras: dev` is the one to understand: **pytest is an
  extra, not a dependency group, and `uv run` installs groups but not extras.**
  Without it the job fails with a bare `Failed to spawn: pytest` after a
  successful-looking install — and only in CI, because locally `make install`
  (`uv sync --extra dev`) has already put pytest in the venv. `make test`
  carried the same hole and hid it, so it now passes the extra too and works on
  a fresh clone. `python-version` is pinned to 3.14 to match the image's base,
  and the reusable workflow runs `uv run --locked`, so a `pyproject.toml`
  edited without its lockfile fails here rather than on someone's machine.
  It is deliberately **not** path filtered: the suite takes seconds, and a
  required check skipped by a path filter never reports, which blocks a PR
  instead of passing it. (`ci-container-build` filters inside the workflow
  instead, behind its always-reporting `container-build` gate.)

  **Coverage is reported and never gated.** `coverage: marketagent` and
  `coverage-pr-comment: true` are inputs on the shared workflow, which puts the
  table in the job summary and in one self-updating PR comment. It lives there
  rather than in this repo because coverage reporting is generic Python CI
  mechanics — an earlier version rode on the `command` input here, which worked
  but meant anyone later wanting `command: pytest -x` would silently lose
  coverage. **`pytest-cov` is deliberately not in the `dev` extra**: the
  workflow injects it with `uv run --with`, an ephemeral overlay that needs no
  dependency and no lockfile churn here and does not invalidate `--locked`.
  Adding it back to `pyproject.toml` would be a no-op that has to be maintained.

  The caller grants `pull-requests: write` for the comment, and that is load
  bearing: a called workflow can never hold more than its caller, so the shared
  workflow declares no permissions of its own. It did at first, and the comment
  would have 403'd on every run.

  There is no `--cov-fail-under` on purpose, because the figure was bimodal
  rather than uniform: the pure modules are at 100% (`risk.py`,
  `models.py`, `benchmarks.py`, `fx.py`, `mailer.py`, `risklimits.py`) and
  everything behind a psycopg connection is far lower — `repository.py` at 27%,
  with 28 of its 30 functions never named in a test, `queries.py` at 37%,
  `jobs/agent.py`'s loop at 32%. That is not neglect; CI has no database, so
  that SQL has nothing to run against. A threshold over the mixture would be
  met by mocking the connection and the Entra token path, which asserts the
  mock rather than the behaviour. **83% overall at 367 tests** is the figure to
  compare against; treat a drop as something to read, not as a build failure.
  Coverage that nothing enforces has to be visible instead, which is the whole
  reason it is surfaced twice rather than left in the log.
- **ci-dashboard**: renders the built SPA in headless Chromium and asserts it
  fits its viewport. **The second workflow here that is not a thin caller of
  `jay-withers/workflows`**, after ci-terraform's `plan` job — there is no
  `node.yml` there to call, and market-agent is the only repo in the estate
  with a SPA (gym-log is server-rendered Jinja2 with no build step). A shared
  workflow with one consumer is a guess at what the second would need; if a
  second frontend appears, this should move there wholesale rather than be
  copied.

  It exists because **nothing in CI drew a page**. Lint, pytest and a docker
  build do not render a pixel, so every CSS change shipped on the strength of
  someone opening it locally — and the fault that prompted it reached
  production and was found on a phone: a `flex-shrink: 0` that reads as
  obviously correct in a diff collapsed the section heading to a column two
  words wide.

  - **The API is stubbed in the browser, not run.** `tests/fixtures.ts` holds
    the response shapes and `page.route` serves them, so the suite needs no
    Postgres, no API image and no network. It is a *layout* suite; wiring it
    to a database would make it a layout suite that only runs when a database
    happens to be up.
  - **Route registration order is load-bearing.** Playwright matches handlers
    in *reverse* registration order, so the catch-all that fails unknown
    `/api/**` requests is registered **first**. Added last it wins over every
    specific stub and the whole page renders its error state — which is
    exactly what happened on the first run.
  - **The fixtures are deliberately awkward**: the longest name on the
    watchlist, a holding with no price at all, a benchmark arm missing from
    the series. A layout only breaks on content that does not fit.
  - **Not screenshot tests.** A pixel baseline fails on every font tweak and
    gets deleted within a month. These assert box geometry — nothing overflows
    the viewport except inside a `.scroll` wrapper, which is what that wrapper
    is for.
  - **Each assertion was checked against the bug.** Reverting the CSS fix
    fails three of them. That caught one assertion that was worthless:
    `controls.y > hint.y` passed even with the bug present, because the row is
    centre-aligned and a squeezed hint is a tall column whose top sits above
    the controls anyway. It now compares against the hint's *bottom*.
  - `npx playwright install` rather than a pinned container image: the CLI
    fetches the browser matching the installed package, so a Renovate bump of
    `@playwright/test` cannot drift away from a hard-coded image tag. The
    browser cache is keyed on `package-lock.json` for the same reason.
  - The status check reports as plain **`Layout`** (the job's `name:`), not
    `<job> / <name>` — that namespacing only applies to reusable-workflow
    calls. It is a required check in `github-repos`.
- **ci-container-build**: builds `apps/marketagent` and `apps/dashboard` on PRs
  touching `apps/**` (decided by a `changes` job, so the required
  `container-build` gate reports on every PR), without pushing, by calling the shared `docker.yml` with
  `push` left at its default of false. The runner is natively amd64, which is
  what Container Apps runs, so this also proves the target architecture builds —
  a local `make build` on Apple Silicon reaches amd64 only through QEMU. Nothing
  else in CI touches these images, so before this a broken Dockerfile surfaced
  only when someone next built by hand.

  **It and cd-publish call the same shared workflow, and that is the point.**
  As two hand-written workflows they were 90% the same file and could drift on
  build context, platform or cache scope — at which point a green pull request
  proves less about the release than it looks like it does. Now the only
  difference between them is `push` and the tags.
- **cd-tag**: auto-creates a semver tag on every merge to `main` via the shared
  `release.yml`, regardless of what changed — a terraform-only or docs-only PR
  gets a version and a GitHub release the same as an app change does. The bump
  size comes purely from conventional-commit type, never from which files
  changed: `feat:`/breaking commits bump minor/major, and everything else
  falls back to `default-bump`, which this repo leaves at its default of
  `patch` rather than `false`. That means `tag` produces a new version for
  **essentially every push** — verified against this repo's actual history,
  34 merged PRs against 34 tags with zero gaps, `chore(deps)` and `docs:`
  merges included. `publish`'s `needs.tag.outputs.tag != ''` guard is real but
  rarely the thing that fires: it only skips publish outright on a push with
  no new commit, or if `default-bump` were ever set to `false`.

  **What `publish` does *not* do unconditionally is rebuild the images.** A
  `changes` job sits between `tag` and `publish` and diffs `apps/` against the
  previous release tag — found with `git describe --tags --abbrev=0 HEAD^`,
  not this push's own `before`/`after` range. That distinction is what makes
  it correct in the rare case `tag` genuinely produces no version: whatever
  `apps/` change such a push carried is still unreleased, and gets picked up
  by whichever *later* push finally does bump one — diffing only that later
  push's own range would silently miss it. Walking back to the actual
  previous tag instead makes the diff cumulative and correct across any
  number of skipped in-between pushes. `publish` is gated on both: a tag
  having been created *and* `changes.outputs.apps == 'true'`. The net effect
  is that a release version can exist with no image ever published under it —
  expected, not a bug — and `cd-publish`'s `workflow_dispatch` is still there
  to publish one by hand if that's ever wanted.
- **cd-publish**: reusable (`workflow_call`) and manual (`workflow_dispatch`),
  delegating the build to the shared `docker.yml` and then the deploy to the
  shared `container-apps-deploy.yml`, which runs `make deploy` and `make smoke`
  and rolls back to `make deployed-tag`'s answer if either fails. The smoke
  test runs `marketagent selfcheck` (`selfcheck.py`) once in every job: keep it
  free of side effects (no order, no LLM, no email), and add a secret to it when
  a job starts reading one, or a release missing it deploys green and fails at
  its first scheduled run. `scripts/smoke.sh` is shared verbatim with gym-log,
  finances, career, repo-agent and security-testing. The dashboard's `/healthz`
  and `/readyz` serve `health.json`, which its entrypoint renders with the
  image tag as `version`; the smoke test needs that field. It stays a workflow of its
  own rather than folding into cd-tag because `workflow_dispatch` is the
  documented way to re-publish an existing tag after a failed build, without
  minting a new version. Both images are pushed under two immutable tags — the
  release `vX.Y.Z` and the commit's short SHA, which is what `IMAGE_TAG` and the
  `agent_runs` rows already use. Neither tag is ever moved: Container Apps
  creates a revision only when the template changes, so a re-pushed moving tag
  would deploy nothing and report success. Only amd64 is built because Container
  Apps runs nothing else.

  **`ref` is the tag, and that is what makes the SHA tag correct.** On a
  `workflow_call` `github.sha` is the calling branch's head rather than the tag,
  so resolving the short SHA from anything but the checkout would record a
  commit that was never built. The shared workflow's `ref`/`tag-with-sha` pair
  is that, and it is why `cd-tag` → `cd-publish` → `docker.yml` nests three
  deep (GitHub allows four).

  **A green publish is not evidence the tags are right — check the registry.**
  The first release through the shared workflow (`v0.6.2`) reported success
  having pushed the short SHA and silently lost the version tag: `read` returns
  non-zero on a final line with no trailing newline and does not run the loop
  body for it, so a single-tag input was dropped whole, and the SHA tag alone
  kept the "no tags resolved" guard from firing. Fixed in `docker.yml` at
  v1.4.1, which now also errors when a non-empty `tags` parses to nothing. The
  habit is still worth keeping after a release:

  ```bash
  gh api user/packages/container/market-agent%2Fmarketagent/versions?per_page=3 \
    --jq '.[] | "\(.updated_at) \(.metadata.container.tags|join(", "))"'
  ```

  A missing version tag does not need a new release: `cd-publish`'s
  `workflow_dispatch` re-publishes an existing tag, and both tags are
  content-addressed to the same image.

## Renovate

`renovate.json` extends the shared presets from
[`jay-withers/renovate`](https://github.com/jay-withers/renovate)
(`github>jay-withers/renovate`) rather than configuring Renovate inline —
that one line pulls in `config:recommended`, semantic commits, automerge/schedule
policy, and per-ecosystem grouping. Only what's genuinely specific to this repo
stays local: `autoApprove` (so Renovate's own PRs clear the branch-protection
review requirement) and two `regexManagers` (`.terraform-version`, and
`.tflint.hcl`'s azurerm plugin version pin) — neither is a GitHub Action, npm/pip
package or Docker image that a shared preset's manager already covers. Change
ecosystem-wide policy in `jay-withers/renovate` itself so every consuming repo picks
it up; only touch this file for something unique here.

## GitHub repo settings

Branch protection and other repo-level GitHub settings aren't templated as files
here. This repo is managed centrally by
[jay-withers/github-repos](https://github.com/jay-withers/github-repos)'s
Terraform root module, which is the single source of truth for every jay-withers
repo.

The required status checks are **`pre-commit / Pre-commit`**, **`test / Test`**,
**`terraform / Terraform`**, **`terraform-plan`**, **`Layout`** and
**`container-build`** — set in that repo's
`terraform/terraform.tfvars`, where this entry sat at
`required_status_checks = []` for as long as the CI existed, so nothing could
actually block a merge. Two rules decide what belongs on that list:

- **A reusable-workflow call reports as `<caller job id> / <reusable job
  name>`**, not the bare job id. Hence `test / Test` rather than `test`, and
  `terraform / Terraform` rather than `terraform`. Read the context off
  `gh pr checks` rather than inferring it from the workflow.
- **Only require a check that always reports.** `ci-container-build` runs on
  every PR and decides in a `changes` job whether to build, so a
  Terraform-only PR skips the build — requiring `build (marketagent)` would
  leave every such PR pending for ever rather than failing it. The
  always-reporting `container-build` gate is what's required instead. The two
  Terraform checks are safe for the same reason: the shared workflow filters
  at the job level so its gate always reports, and `terraform-plan` is
  `if: always()` and treats skipped as success.

**Why Terraform takes two contexts rather than one.** `terraform / Terraform`
is the shared workflow's gate and can only see the jobs inside it; `plan` lives
in this repo (see the ci-terraform entry for why), so it needs a gate of its
own or a failed plan could not block a merge. Dropping either one silently
stops guarding half of the Terraform CI.

**This is enforced.** The contexts above are on this repo's ruleset. Read the
live state with:

```bash
gh api repos/jay-withers/market-agent/rulesets --jq '.[].id' \
  | xargs -I{} gh api repos/jay-withers/market-agent/rulesets/{} \
    --jq '[.rules[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context]'
```

**The hazard, if this list is ever changed, is the apply, not the rename.**
Applying a list that names a context nothing reports leaves every PR *pending*
rather than failing it — which is precisely what happened to `github-repos`
itself at one point: its live ruleset required `ci-terraform` while its own CI
reported `terraform / Terraform`, blocking its own pull requests until its
tfvars (already correct) was actually applied. So check the contexts in tfvars
against `gh pr checks` output *before* applying, not after.

A local `make apply` there, from a checkout of the branch, is still the
break-glass way to unstick a stale required check without an admin bypass.

Note that the ruleset sets `strict_required_status_checks_policy = true`, so a
PR must be up to date with `main` before it can merge. `github-repos` applies
through its own `cd-terraform`: a merge plans and opens a `terraform-pending`
issue, and a change there takes effect when someone runs that workflow by
hand.
