# Basic example — the configuration ci-terraform plans against.
#
# environment selects which tfvars file to plan with, e.g.
# `terraform plan -var-file=../../environments/dev.tfvars` — see
# terraform/environments/{dev,stg,prd}.tfvars.

module "basic" {
  source = "../../"

  environment = var.environment
}
