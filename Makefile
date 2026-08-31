TF_DIR := terraform
EXAMPLE_DIR := $(TF_DIR)/examples/basic
ENV ?= dev

.DEFAULT_GOAL := help

.PHONY: help install lint init fmt validate plan test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install pre-commit hooks (run once after cloning)
	pre-commit install
	pre-commit install --hook-type commit-msg

lint: ## Run all pre-commit hooks against every file
	pre-commit run --all-files

init: ## terraform init
	terraform -chdir=$(TF_DIR) init

fmt: ## terraform fmt -recursive
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: init ## terraform init + validate
	terraform -chdir=$(TF_DIR) validate

plan: ## terraform init + plan against the basic example (set ENV=dev|stg|prd, default dev)
	terraform -chdir=$(EXAMPLE_DIR) init
	terraform -chdir=$(EXAMPLE_DIR) plan -var-file=../../environments/$(ENV).tfvars

test: init ## terraform test (mocked azurerm provider — no Azure auth)
	terraform -chdir=$(TF_DIR) test
