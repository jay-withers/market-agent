# Backend configuration for the prd environment.
#
#   terraform init -backend-config=backends/prd.hcl
#
# See backends/dev.hcl for what these values are and why they aren't secret.
# Note prd is planned in CI but not currently applied — see "Environments" in
# the README. "Production" here still means paper trading only.
resource_group_name  = "rg-tfstate-shared"
storage_account_name = "sttfsharedjw"
container_name       = "market-agent"
key                  = "prd.terraform.tfstate"

use_oidc         = true
use_azuread_auth = true
