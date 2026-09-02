TF_DIR := terraform
APP_DIR := apps/investagent
ENV ?= dev

# Deterministic by convention — the naming module carries no random suffix — so
# hardcoding the dev vault is safe, and overridable for anything else.
KEY_VAULT_URI ?= https://kv-marketagent-dev.vault.azure.net/

.DEFAULT_GOAL := help

.PHONY: help install lint test secrets sql up down logs run-agent init fmt validate plan apply

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
	uv run --directory $(APP_DIR) pytest

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

lint: ## Run all pre-commit hooks against every file
	pre-commit run --all-files

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
