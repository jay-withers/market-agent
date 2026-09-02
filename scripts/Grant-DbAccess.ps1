<#
.SYNOPSIS
    Grants the workload's managed identity access to the PostgreSQL database.

.DESCRIPTION
    This is the one step Terraform cannot do: creating a database principal for a
    managed identity is SQL (pgaadauth_create_principal), not an Azure API call,
    and the azurerm provider has no way to execute it. Until it runs, containers
    start cleanly and then fail every query.

    Safe to re-run: the principal is created only if absent, and GRANTs are
    idempotent.

    Connects as the signed-in user, who must be the server's Entra administrator
    (Terraform makes whoever applied it the administrator by default). The server
    has no password — an Entra access token is sent in its place.

    Requires the PostgreSQL client tools (psql) and az on PATH. terraform and git
    are needed only to fill in whatever wasn't passed as a parameter.

.PARAMETER TerraformDir
    Directory holding the root configuration. Defaults to `terraform/` in the
    repository root, located with git.

.PARAMETER ResourceGroupName
    Resource group holding the server. Defaults to the `resource_group_name`
    Terraform output.

.PARAMETER ServerFqdn
    Hostname of the server. Defaults to the `postgres_fqdn` Terraform output.

.PARAMETER DatabaseName
    Database to grant access to. Defaults to the `postgres_database_name`
    Terraform output.

.PARAMETER IdentityName
    Name of the managed identity to create a database principal for. Defaults to
    the `identity_name` Terraform output.

.PARAMETER AdminUpn
    Entra administrator to connect as. Defaults to the signed-in user, which is
    who the administrator is meant to be — pass it only if the directory lookup
    is blocked.

.EXAMPLE
    ./scripts/Grant-DbAccess.ps1

    Reads everything from Terraform outputs, which needs the state backend.

.EXAMPLE
    ./scripts/Grant-DbAccess.ps1 -ResourceGroupName rg-marketagent-dev `
        -ServerFqdn psql-marketagent-dev.postgres.database.azure.com `
        -DatabaseName psqldb-marketagent-dev -IdentityName uai-marketagent-dev

    Passing all four skips Terraform entirely — no state access, no init.
#>
[CmdletBinding()]
param(
    [string]$TerraformDir,
    [string]$ResourceGroupName,
    [string]$ServerFqdn,
    [string]$DatabaseName,
    [string]$IdentityName,
    [string]$AdminUpn
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Native commands signal failure through the exit code, not by throwing, and
# PowerShell ignores that by default before 7.4. This wraps the calls whose
# output we capture and use as a value.
function Get-NativeOutput {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $output = & $Command @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($Arguments -join ' ') failed with exit code ${LASTEXITCODE}:`n$output"
    }
    return ($output | Out-String).Trim()
}

function Assert-Tool {
    param([Parameter(Mandatory)][string]$Name)

    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        return
    }
    if ($Name -eq 'psql') {
        throw "psql not found. Install the PostgreSQL client tools, e.g. 'winget install PostgreSQL.PostgreSQL', 'brew install libpq' or 'apt-get install postgresql-client'."
    }
    throw "$Name not found on PATH."
}

# Checked here because every run needs them. terraform and git are checked
# further down instead: they're needed only to fill in an omitted parameter, so
# passing all four means neither has to be installed.
Assert-Tool az
Assert-Tool psql

function Get-TfOutput {
    param([Parameter(Mandatory)][string]$Name)
    # -chdir only accepts the equals form; passing the path as a separate
    # argument is rejected outright.
    Get-NativeOutput terraform @("-chdir=$TerraformDir", 'output', '-raw', $Name)
}

if (-not ($ResourceGroupName -and $ServerFqdn -and $DatabaseName -and $IdentityName)) {
    Assert-Tool terraform

    if (-not $TerraformDir) {
        Assert-Tool git
        $TerraformDir = Join-Path (Get-NativeOutput git @('rev-parse', '--show-toplevel')) 'terraform'
    }

    if (-not $ResourceGroupName) { $ResourceGroupName = Get-TfOutput 'resource_group_name' }
    if (-not $ServerFqdn) { $ServerFqdn = Get-TfOutput 'postgres_fqdn' }
    if (-not $DatabaseName) { $DatabaseName = Get-TfOutput 'postgres_database_name' }
    if (-not $IdentityName) { $IdentityName = Get-TfOutput 'identity_name' }
}

$server = $ServerFqdn.Split('.')[0]

if (-not $AdminUpn) {
    $AdminUpn = Get-NativeOutput az @('ad', 'signed-in-user', 'show', '--query', 'userPrincipalName', '-o', 'tsv')
}

# The server's only firewall rule is the "allow Azure services" sentinel, which
# does not cover this machine.
$myIp = (Invoke-RestMethod -Uri 'https://api.ipify.org').ToString().Trim()
$rule = "tmp-grant-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"

# Passed to psql as a file rather than on stdin or via -c: -c rejects the \echo
# meta-command, and a file keeps the quoting entirely out of PowerShell's hands.
# Identifiers interpolate as :"name" and literals as :'name', so psql quotes them.
$sqlFile = New-TemporaryFile
Set-Content -LiteralPath $sqlFile -Encoding utf8 -Value @'
SELECT pgaadauth_create_principal(:'ident', false, false)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ident');

GRANT CONNECT ON DATABASE :"db" TO :"ident";
GRANT USAGE, CREATE ON SCHEMA public TO :"ident";

-- Default privileges rather than a one-off GRANT ON ALL TABLES: the latter
-- covers only tables that exist right now, so anything a later migration
-- creates would be invisible to the workload. Migrations run as this identity
-- and so own their tables outright, but this keeps a migration run by hand as
-- the administrator from silently locking the app out.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"ident";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"ident";

\echo '==> principal:'
SELECT rolname FROM pg_roles WHERE rolname = :'ident';
'@

Write-Host "==> adding temporary firewall rule $rule for $myIp"
Get-NativeOutput az @(
    'postgres', 'flexible-server', 'firewall-rule', 'create',
    '--resource-group', $ResourceGroupName, '--name', $server,
    '--rule-name', $rule,
    '--start-ip-address', $myIp, '--end-ip-address', $myIp
) | Out-Null

# finally, not a trap: the rule is removed whatever happens, including Ctrl-C, so
# a broken run doesn't leave the database reachable from an address nobody
# remembers granting.
try {
    # Token as password, not a secret: scoped to the Postgres resource and
    # expires in about an hour.
    $env:PGPASSWORD = Get-NativeOutput az @(
        'account', 'get-access-token', '--resource-type', 'oss-rdbms',
        '--query', 'accessToken', '-o', 'tsv'
    )

    Write-Host "==> granting $IdentityName access to $DatabaseName"

    # Called directly rather than through Get-NativeOutput so psql's results
    # stream to the console. ON_ERROR_STOP makes a failed statement fail the run
    # rather than scrolling past.
    & psql `
        --set=ON_ERROR_STOP=1 `
        "--set=ident=$IdentityName" `
        "--set=db=$DatabaseName" `
        --file $sqlFile `
        "host=$ServerFqdn dbname=$DatabaseName user=$AdminUpn sslmode=require"

    if ($LASTEXITCODE -ne 0) {
        throw "psql failed with exit code $LASTEXITCODE"
    }

    Write-Host '==> done'
}
finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sqlFile -ErrorAction SilentlyContinue

    Write-Host "==> removing temporary firewall rule $rule"
    & az postgres flexible-server firewall-rule delete `
        --resource-group $ResourceGroupName --name $server `
        --rule-name $rule --yes 2>&1 | Out-Null
}
