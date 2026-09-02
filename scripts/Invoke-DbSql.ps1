<#
.SYNOPSIS
    Runs a SQL file against the PostgreSQL server as its Entra administrator.

.DESCRIPTION
    Connection plumbing only: a temporary firewall rule for this machine, an
    Entra access token in place of the password (the server has none), and psql.
    The files in sql/ at the repository root name their own identities and
    databases, so this script knows nothing about what it runs — add a file
    there per task.

    Defaults are the dev names. Resource names are deterministic by convention
    (no random suffix), so hardcoding them is safe and keeps Terraform, its
    state backend and git out of the picture entirely.

    Requires psql and az on PATH.

.EXAMPLE
    ./scripts/Invoke-DbSql.ps1

    Grants the managed identity database access — the default file.

.EXAMPLE
    ./scripts/Invoke-DbSql.ps1 -SqlFile ./sql/some-other-task.sql
#>
[CmdletBinding()]
param(
    # $PSScriptRoot is scripts/, so the default climbs to the repository root
    # rather than depending on the working directory.
    [string]$SqlFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'sql' 'grant-uai-access.sql'),
    [string]$ResourceGroupName = 'rg-marketagent-dev',
    [string]$ServerFqdn = 'psql-marketagent-dev.postgres.database.azure.com',
    [string]$DatabaseName = 'psqldb-marketagent-dev',
    [string]$AdminUpn
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Native commands report failure by exit code, not by throwing, and PowerShell
# ignores that unless asked — still off by default in 7.6, so this is opt-in
# rather than something a modern pwsh gives us for free.
$PSNativeCommandUseErrorActionPreference = $true

# Checked by hand only because the install is per-platform and not obvious; az
# and psql failing to launch is self-explanatory otherwise.
if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    throw "psql not found. Install the PostgreSQL client tools, e.g. 'winget install PostgreSQL.PostgreSQL', 'brew install libpq' or 'apt-get install postgresql-client'."
}
if (-not (Test-Path -LiteralPath $SqlFile)) {
    throw "SQL file not found: $SqlFile"
}

if (-not $AdminUpn) {
    $AdminUpn = az ad signed-in-user show --query userPrincipalName -o tsv
}

# The server's only permanent rule is the "allow Azure services" sentinel, which
# does not cover this machine.
$server = $ServerFqdn.Split('.')[0]
$myIp = (Invoke-RestMethod -Uri 'https://api.ipify.org').ToString().Trim()
$rule = "tmp-sql-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"

Write-Host "==> adding temporary firewall rule $rule for $myIp"
az postgres flexible-server firewall-rule create `
    --resource-group $ResourceGroupName --name $server --rule-name $rule `
    --start-ip-address $myIp --end-ip-address $myIp | Out-Null

# finally, not a trap: the rule is removed whatever happens, including Ctrl-C, so
# a broken run doesn't leave the database reachable from an address nobody
# remembers granting.
try {
    # Token as password, not a secret: scoped to the Postgres resource and
    # expires in about an hour.
    $env:PGPASSWORD = az account get-access-token `
        --resource-type oss-rdbms --query accessToken -o tsv

    Write-Host "==> running $(Split-Path -Leaf $SqlFile) against $DatabaseName"

    # ON_ERROR_STOP makes a failed statement fail the run rather than scrolling
    # past; psql then exits non-zero and the preference above throws.
    psql --set=ON_ERROR_STOP=1 --file $SqlFile `
        "host=$ServerFqdn dbname=$DatabaseName user=$AdminUpn sslmode=require"

    Write-Host '==> done'
}
finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

    Write-Host "==> removing temporary firewall rule $rule"
    # Warned about rather than thrown: an exception raised here would replace
    # whatever actually went wrong above, and the rule still needs removing.
    $PSNativeCommandUseErrorActionPreference = $false
    az postgres flexible-server firewall-rule delete `
        --resource-group $ResourceGroupName --name $server `
        --rule-name $rule --yes 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "could not remove firewall rule $rule — remove it by hand"
    }
}
