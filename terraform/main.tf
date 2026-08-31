# Names for the singleton resources; the container workloads use their own
# instances in main.container-apps*.tf.
#
# This module truncates names from the right with no room reserved for its
# 4-character random suffix, so a name at the limit silently loses uniqueness.
# Key Vault is the tight one: kv-investagent-dev-XXXX is 23 of 24 allowed
# characters. Check with `terraform console` before lengthening project_name or
# using longer environment names.
module "naming" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver
  # (version below), not a git source — there's no commit hash to pin.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment]
}

resource "azurerm_resource_group" "this" {
  name     = module.naming.resource_group.name_unique
  location = var.location
  tags     = local.tags
}
