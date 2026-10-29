# Names for the singleton resources; the container workloads use their own
# instances in main.container-apps*.tf.
#
# Deliberately `.name`, not `.name_unique`: names are deterministic, with no
# random suffix. That means the Key Vault and SQL server names — which are
# globally unique across Azure, not just within this subscription — can in
# principle collide with someone else's. If an apply fails on a name already in
# use, change var.project_name rather than reaching for `.name_unique`.
#
# Names are still truncated at each resource type's limit (Key Vault is the
# tightest at 24 characters), so check with `terraform console` before
# lengthening project_name.
module "naming" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver
  # (version below), not a git source — there's no commit hash to pin.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment]
}

# The resource group is github-repos' (`create_resource_groups`), which
# imported the one this config's first apply made, and grants the pipeline
# Contributor and conditioned RBAC Administrator on it. Dropped from this state
# without touching Azure; data.tf looks it up by the same name.
removed {
  from = azurerm_resource_group.this

  lifecycle {
    destroy = false
  }
}
