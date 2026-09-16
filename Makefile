TF_DIR := terraform
APP_DIR := apps/investagent
ENV ?= dev

# How many log lines logs-azure-history pulls back.
LINES ?= 200

# Deterministic by convention — the naming module carries no random suffix — so
# hardcoding the dev vault is safe, and overridable for anything else.
KEY_VAULT_URI ?= https://kv-marketagent-dev.vault.azure.net/

.DEFAULT_GOAL := help

.PHONY: help install lint test test-db secrets sql up down logs run-agent run-weekly build push deploy logs-azure init fmt validate plan apply

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Expected to be re-run after a dev container rebuild, not just after a clone:
# uv installs into ~/.local/bin, which is the container's writable layer and
# does not survive one.
install: ## Install pre-commit hooks and Python dependencies
	pre-commit install
	pre-commit install --hook-type commit-msg
	command -v uv >/dev/null || curl -fsSL https://astral.sh/uv/install.sh | sh
	uv sync --directory $(APP_DIR) --extra dev

test: ## Run the Python test suite
	# --extra dev: pytest is an extra, not a dependency group, so `uv run` does
	# not install it. Without this the target only works after `make install`.
	uv run --directory $(APP_DIR) --extra dev pytest

# The database tests skip without POSTGRES_TEST_DSN, so `make test` stays
# runnable on a clone with no Docker and this is the target that adds them.
#
# host.docker.internal rather than localhost: the Docker daemon runs on the
# *host* (docker-outside-of-docker), so compose publishes 5432 there and not
# into this container. The suite creates and drops its own scratch database, so
# it never touches rows the compose stack is holding.
test-db: ## Run the Python test suite including the database tests (needs `make up`)
	POSTGRES_TEST_DSN="postgresql://postgres:local@host.docker.internal:5432/postgres" \
		uv run --directory $(APP_DIR) --extra dev pytest

secrets: ## Prompt for the application secrets and store them in Key Vault
	./scripts/Set-KeyVaultSecrets.ps1

sql: ## Run every SQL file in sql/ against the database, in filename order
	./scripts/Invoke-DbSql.ps1

# The local loop: Postgres with the schema baked in, plus the API. No Azure.
# Note the published ports land on the Docker *host* — from inside the dev
# container, reach a service at its bridge IP or via host.docker.internal.
up: ## Start the local stack (Postgres + API) with docker compose
	docker compose up -d --build

down: ## Stop the local stack and delete its data volume
	docker compose down -v

logs: ## Follow the local stack's logs
	docker compose logs -f

# `run --rm`, not a long-running service: the agent is a scheduled job, and a
# container that restarted would trade again each time.
#
# Local database, but *real* calls to Alpaca, Frankfurter and Anthropic — the
# last of which is billed. It places no order, because DRY_RUN is true in the
# compose environment.
#
# Secrets come from Key Vault rather than a file. The image has no `az`, so the
# token is minted here and passed in: it lasts about an hour and is scoped to
# Key Vault alone, which is a far better thing to hand a container than four
# long-lived API keys in a .env. Falls back to .env if there is no az login.
run-agent: ## Run the agent once against the local stack (real APIs, no orders)
	KEY_VAULT_URI=$(KEY_VAULT_URI) \
	AZURE_KEYVAULT_TOKEN="$$(az account get-access-token \
	  --resource https://vault.azure.net --query accessToken -o tsv 2>/dev/null)" \
	docker compose run --rm agent

# Same token dance as run-agent. Reads only — no market data, no broker, one
# model call — so this is safe to run repeatedly against the local stack; it
# will simply review a week in which nothing much happened.
run-weekly: ## Run the weekly review once against the local stack
	KEY_VAULT_URI=$(KEY_VAULT_URI) \
	AZURE_KEYVAULT_TOKEN="$$(az account get-access-token \
	  --resource https://vault.azure.net --query accessToken -o tsv 2>/dev/null)" \
	docker compose run --rm weekly

lint: ## Run all pre-commit hooks against every file
	pre-commit run --all-files

# These targets are for local iteration and for testing an image before it is
# merged. The release path is CI: cd-tag mints the version on merge to main and
# cd-publish builds both images on a native amd64 runner, pushing each under the
# release tag and the commit's short SHA. Deploy a published tag with
# `make deploy IMAGE_TAG=v1.2.3`.
#
# --platform linux/amd64 is not optional here. Container Apps runs amd64 only,
# and this dev host is arm64 (Docker Desktop on Apple Silicon): a native build
# deploys an image that crash-loops with an exec format error and no other
# clue. buildx emulates, so it is slower than a native build — which is most of
# why building in CI is worth having.
#
# The tag is the short git SHA, and it must be immutable — Container Apps only
# creates a revision when the template changes, so a re-pushed moving tag
# deploys nothing and reports success.
IMAGE_TAG ?= $(shell git rev-parse --short HEAD)
IMAGE_REGISTRY ?= ghcr.io/jay-withers/market-agent

# `make deploy` sets both from IMAGE_TAG by default, so the tag built stays the
# tag deployed and the two cannot drift. Override one of these only to deploy
# the images at different versions — rolling the dashboard back to yesterday's
# release while the API stays on today's, say.
INVESTAGENT_IMAGE_TAG ?= $(IMAGE_TAG)
DASHBOARD_IMAGE_TAG ?= $(IMAGE_TAG)

# Whether each tag was actually passed by the caller (command line or
# environment) rather than falling back to its `?=` default — `origin` reports
# "file" only for the latter. `deploy` requires this: unlike `build`/`push`,
# where the local git SHA is exactly what you want to iterate fast, silently
# deploying whatever commit happens to be checked out is how a stale or
# never-pushed local SHA ends up rolled out to Container Apps.
IMAGE_TAG_EXPLICIT := $(filter-out file,$(origin IMAGE_TAG))
INVESTAGENT_TAG_EXPLICIT := $(filter-out file,$(origin INVESTAGENT_IMAGE_TAG))
DASHBOARD_TAG_EXPLICIT := $(filter-out file,$(origin DASHBOARD_IMAGE_TAG))

build: ## Build both images for linux/amd64 (set IMAGE_TAG, default: git sha)
	docker buildx build --platform linux/amd64 \
	  -t $(IMAGE_REGISTRY)/investagent:$(IMAGE_TAG) \
	  --load ./apps/investagent
	docker buildx build --platform linux/amd64 \
	  -t $(IMAGE_REGISTRY)/dashboard:$(IMAGE_TAG) \
	  --load ./apps/dashboard

# Needs a token with write:packages — `gh auth refresh --scopes write:packages,read:packages`.
# ghcr defaults a new package to private regardless of repository visibility, so
# both need flipping to public once after the first push or Container Apps
# cannot pull them.
push: ## Push both images to ghcr.io (needs write:packages)
	gh auth token | docker login ghcr.io -u $$(gh api user --jq .login) --password-stdin
	docker push $(IMAGE_REGISTRY)/investagent:$(IMAGE_TAG)
	docker push $(IMAGE_REGISTRY)/dashboard:$(IMAGE_TAG)

# Deploys via az cli, not terraform apply: the image and IMAGE_TAG on every
# container app and job are lifecycle.ignore_changes'd in Terraform (see
# main.container-apps.tf / main.container-apps-jobs.tf), specifically so this
# can move independently of a plan/apply cycle. `make apply` still seeds the
# *first* revision of a brand-new environment from investagent_image_tag /
# dashboard_image_tag; every deploy after that is this target.
#
# --set-env-vars only adds/updates the name(s) given — it does not touch any
# other environment variable already on the container (unlike
# --replace-env-vars) — so IMAGE_TAG moves without disturbing KEY_VAULT_URI,
# POSTGRES_HOST and the rest of common_env.
#
# Same immutable-tag guard as the (now-unused-for-this-path) Terraform
# validation: a `latest`/`main`/`unset` tag would report success here while
# Container Apps creates no new revision at all, because the template hasn't
# changed from its perspective.
deploy: ## Deploy built images to Container Apps via az cli (needs IMAGE_TAG=vX.Y.Z; set ENV, default dev)
	@if [ -z "$(IMAGE_TAG_EXPLICIT)" ] && { [ -z "$(INVESTAGENT_TAG_EXPLICIT)" ] || [ -z "$(DASHBOARD_TAG_EXPLICIT)" ]; }; then \
	  echo "error: make deploy needs an explicit tag — pass IMAGE_TAG=vX.Y.Z (or INVESTAGENT_IMAGE_TAG=... and DASHBOARD_IMAGE_TAG=... to deploy them separately), not the git-SHA default" >&2; \
	  exit 1; \
	fi
	@for tag in "$(INVESTAGENT_IMAGE_TAG)" "$(DASHBOARD_IMAGE_TAG)"; do \
	  case "$$tag" in \
	    latest|main|unset) \
	      echo "error: image tag must be immutable, got '$$tag' — pass IMAGE_TAG=\$$(git rev-parse --short HEAD) or a release tag" >&2; \
	      exit 1;; \
	  esac; \
	done
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	RG=$$(terraform -chdir=$(TF_DIR) output -raw resource_group_name); \
	API=$$(terraform -chdir=$(TF_DIR) output -raw api_app_name); \
	DASHBOARD=$$(terraform -chdir=$(TF_DIR) output -raw dashboard_app_name); \
	AGENT=$$(terraform -chdir=$(TF_DIR) output -raw agent_job_name); \
	SUMMARY=$$(terraform -chdir=$(TF_DIR) output -raw summary_job_name); \
	WEEKLY=$$(terraform -chdir=$(TF_DIR) output -raw weekly_review_job_name); \
	az containerapp update --name $$API --resource-group $$RG \
	  --image $(IMAGE_REGISTRY)/investagent:$(INVESTAGENT_IMAGE_TAG) \
	  --set-env-vars IMAGE_TAG=$(INVESTAGENT_IMAGE_TAG); \
	az containerapp update --name $$DASHBOARD --resource-group $$RG \
	  --image $(IMAGE_REGISTRY)/dashboard:$(DASHBOARD_IMAGE_TAG); \
	for JOB in $$AGENT $$SUMMARY $$WEEKLY; do \
	  az containerapp job update --name $$JOB --resource-group $$RG \
	    --image $(IMAGE_REGISTRY)/investagent:$(INVESTAGENT_IMAGE_TAG) \
	    --set-env-vars IMAGE_TAG=$(INVESTAGENT_IMAGE_TAG); \
	done

# --container is mandatory here, and it names the container inside the job
# (`agent`), not the job itself. Streaming only works while an execution has a
# live replica: a finished run keeps none, and the CLI then fails with "No
# replicas found for execution". Use logs-azure-history for a run that is over.
logs-azure: ## Tail the agent job's logs in Azure (only while a run is in flight)
	az containerapp job logs show \
	  --name $$(terraform -chdir=$(TF_DIR) output -raw agent_job_name) \
	  --resource-group $$(terraform -chdir=$(TF_DIR) output -raw resource_group_name) \
	  --container agent \
	  --follow

# Log Analytics keeps what the replicas don't, so this is the one that works
# after a scheduled run has finished. Ingestion lags by a few minutes, which is
# why it complements the live tail rather than replacing it.
logs-azure-history: ## The agent job's recent logs from Log Analytics (LINES=200)
	az monitor log-analytics query \
	  --workspace $$(az monitor log-analytics workspace list \
	    --resource-group $$(terraform -chdir=$(TF_DIR) output -raw resource_group_name) \
	    --query "[0].customerId" -o tsv) \
	  --analytics-query "ContainerAppConsoleLogs_CL | where ContainerName_s == 'agent' | top $(LINES) by TimeGenerated desc | order by TimeGenerated asc | project TimeGenerated, Log_s" \
	  -o table

# -backend=false: the azurerm backend is configured partially (see
# terraform/backends/), so a plain init would prompt for the missing values.
# Anything that doesn't touch state can skip the backend entirely.
init: ## terraform init, without configuring the state backend
	terraform -chdir=$(TF_DIR) init -backend=false

fmt: ## terraform fmt -recursive
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: init ## terraform init + validate (no Azure credentials needed)
	terraform -chdir=$(TF_DIR) validate

plan: ## terraform init + plan (set ENV=dev|stg|prd, default dev)
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) plan -var-file=environments/$(ENV).tfvars

# No -auto-approve: this creates resources that bill, so the plan is worth
# reading. Only dev is expected to be applied — see "Environments" in the README.
apply: ## terraform init + apply (set ENV=dev|stg|prd, default dev)
	terraform -chdir=$(TF_DIR) init -reconfigure -backend-config=backends/$(ENV).hcl
	terraform -chdir=$(TF_DIR) apply -var-file=environments/$(ENV).tfvars
