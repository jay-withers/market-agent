# Backend configuration for the stg environment.
#
#   terraform init -backend-config=backends/stg.hcl
#
# See backends/dev.hcl for what these values are and why they aren't secret.
# Note stg is planned in CI but not currently applied — see "Environments" in
# the README.
resource_group_name  = "rg-tfstate-shared"
storage_account_name = "sttfsharedjw"
container_name       = "market-agent"
key                  = "stg.terraform.tfstate"

use_oidc         = true
use_azuread_auth = true
