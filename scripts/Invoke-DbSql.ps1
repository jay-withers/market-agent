#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Runs SQL files against the PostgreSQL server as its Entra administrator.

.DESCRIPTION
    Connection plumbing only: a temporary firewall rule for this machine, an
    Entra access token in place of the password (the server has none), and psql.
    The files in sql/ at the repository root name their own identities and
    databases, so this script knows nothing about what it runs — add a file
    there per task.

    Defaults are the dev names. Resource names are deterministic by convention
    (no random suffix), so hardcoding them is safe and keeps Terraform, its
    state backend and git out of the picture entirely.

    The firewall rule is named after this machine's public IP and reused if it
    already exists, and removal is confirmed rather than automatic: each
    firewall change takes a while and the server rejects a second one while the
    first is still processing, so keeping the rule makes a debug-and-retry loop
    substantially quicker.

    Everything given to -Path runs under one firewall rule and one access
    token, which is the whole reason for accepting more than one file: a rule
    per migration would spend most of the run waiting on the server, and a
    second change while the first is still processing fails outright.

    Requires psql and az on PATH.

.PARAMETER Path
    Files and/or directories to run, in the order given. A directory expands to
    its *.sql files sorted by name, which is what the numeric filename prefixes
    in sql/ are for. Defaults to sql/ in its entirety — every file there is
    idempotent, so that is "bring the database up to date".

.PARAMETER KeepFirewallRule
    Leave the firewall rule in place without asking. Implied when input is
    redirected, since there is nobody to answer the prompt.

.EXAMPLE
    ./scripts/Invoke-DbSql.ps1

    Runs everything in sql/ in filename order.

.EXAMPLE
    ./scripts/Invoke-DbSql.ps1 -Path ./sql/001-schema.sql -KeepFirewallRule

    Runs one file and keeps the rule for the next attempt.
#>
[CmdletBinding()]
param(
    # $PSScriptRoot is scripts/, so the default climbs to the repository root
    # rather than depending on the working directory.
    [Alias('SqlFile')]
    [string[]]$Path = (Join-Path (Split-Path -Parent $PSScriptRoot) 'sql'),
    [string]$ResourceGroupName = 'rg-marketagent-dev',
    [string]$ServerFqdn = 'psql-marketagent-dev.postgres.database.azure.com',
    [string]$DatabaseName = 'psqldb-marketagent-dev',
    [string]$AdminUpn,
    [switch]$KeepFirewallRule
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
# Resolved before the firewall rule is touched: a typo in a path should cost
# nothing, and a partially-run batch is the expensive failure to avoid.
$sqlFiles = @(foreach ($entry in $Path) {
        if (-not (Test-Path -LiteralPath $entry)) {
            throw "SQL path not found: $entry"
        }
        $item = Get-Item -LiteralPath $entry
        if ($item.PSIsContainer) {
            $found = @(Get-ChildItem -LiteralPath $item.FullName -Filter '*.sql' -File | Sort-Object Name)
            if ($found.Count -eq 0) {
                throw "No .sql files in $($item.FullName)"
            }
            $found.FullName
        }
        else {
            $item.FullName
        }
    })

Write-Host "==> $($sqlFiles.Count) file(s) to run, in order:"
$sqlFiles | ForEach-Object { Write-Host "    $(Split-Path -Leaf $_)" }

if (-not $AdminUpn) {
    $AdminUpn = az ad signed-in-user show --query userPrincipalName -o tsv
}

# The server's only permanent rule is the "allow Azure services" sentinel, which
# does not cover this machine.
$server = $ServerFqdn.Split('.')[0]
$myIp = (Invoke-RestMethod -Uri 'https://api.ipify.org').ToString().Trim()

# Named after the IP rather than the clock so a rule kept from an earlier run is
# reused instead of leaving one rule behind per invocation.
$rule = "devbox-$($myIp -replace '\.', '-')"

$PSNativeCommandUseErrorActionPreference = $false
az postgres flexible-server firewall-rule show `
    --resource-group $ResourceGroupName --name $server --rule-name $rule 2>&1 | Out-Null
$ruleExisted = $LASTEXITCODE -eq 0
$PSNativeCommandUseErrorActionPreference = $true

if ($ruleExisted) {
    Write-Host "==> reusing firewall rule $rule for $myIp"
}
else {
    Write-Host "==> adding firewall rule $rule for $myIp"
    az postgres flexible-server firewall-rule create `
        --resource-group $ResourceGroupName --name $server --rule-name $rule `
        --start-ip-address $myIp --end-ip-address $myIp | Out-Null
}

# finally, not a trap: the prompt is reached whatever happens, including Ctrl-C,
# so a broken run can't quietly leave the database reachable from an address
# nobody remembers granting.
try {
    # Token as password, not a secret: scoped to the Postgres resource and
    # expires in about an hour. Fetched once and reused by every file below —
    # long enough for any batch this repo will have.
    $env:PGPASSWORD = az account get-access-token `
        --resource-type oss-rdbms --query accessToken -o tsv

    # A psql process per file rather than one process for all of them: files
    # are allowed to \connect elsewhere (grant-uai-access.sql has to), and
    # ON_ERROR_STOP only aborts the process it is set on, so concatenating them
    # would let a later file run against the wrong database or after a failure.
    foreach ($file in $sqlFiles) {
        Write-Host "==> running $(Split-Path -Leaf $file) against $DatabaseName"

        # ON_ERROR_STOP makes a failed statement fail the run rather than
        # scrolling past; psql then exits non-zero and the preference above
        # throws, which skips the remaining files.
        psql --set=ON_ERROR_STOP=1 --file $file `
            "host=$ServerFqdn dbname=$DatabaseName user=$AdminUpn sslmode=require"
    }

    Write-Host "==> done ($($sqlFiles.Count) file(s))"
}
finally {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

    # Redirected input means nobody can answer, so keep the rule rather than
    # blocking forever — the message below says how to remove it.
    $keep = $KeepFirewallRule -or [Console]::IsInputRedirected
    if (-not $keep) {
        $keep = (Read-Host "remove firewall rule $rule for $myIp? [Y/n]").Trim() -match '^n'
    }

    if ($keep) {
        Write-Host "==> keeping firewall rule $rule. Remove it with:"
        Write-Host "    az postgres flexible-server firewall-rule delete --resource-group $ResourceGroupName --name $server --rule-name $rule --yes"
    }
    else {
        Write-Host "==> removing firewall rule $rule"
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
}
