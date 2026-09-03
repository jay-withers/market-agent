# Backend configuration for the dev environment.
#
#   terraform init -backend-config=backends/dev.hcl
#
# These values are not secret: the state container is protected by Azure RBAC,
# not by keeping its name private. The storage account is created once by hand —
# see "Terraform state" in the README — and storage account names are globally
# unique, so the name below must be changed to whatever you created.
resource_group_name  = "rg-tfstate-shared"
storage_account_name = "sttfsharedjw"
container_name       = "market-agent"
key                  = "dev.terraform.tfstate"

# OIDC in CI; Entra auth rather than a storage account access key.
use_oidc         = true
use_azuread_auth = true
