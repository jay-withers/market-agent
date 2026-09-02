#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Prompts for the application's secret values and stores them in Key Vault.

.DESCRIPTION
    Terraform owns the vault and its RBAC but deliberately owns none of the
    values, so populating them is a manual step. This is that step, made
    repeatable.

    Values are read from a masked prompt, held only as a SecureString until the
    moment they are passed to `az`, and never written to disk, logged, echoed,
    or placed in shell history. Press Enter at a prompt to skip that secret —
    they are needed at different points in the build (Anthropic and the two
    Alpaca keys for the agent, Resend only for the daily summary), so there is
    no need to have all four to hand.

    Existing secrets are left alone unless you say otherwise, because Key Vault
    versions every write and re-setting an unchanged value just adds noise.

    Requires az on PATH, a completed `az login`, and `Key Vault Secrets
    Officer` on the vault — which whoever ran `terraform apply` already has.

.PARAMETER Name
    Which secrets to prompt for. Defaults to all four the application uses.

.PARAMETER Force
    Overwrite an existing secret without asking.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1

    Prompts for each of the four, skipping any that already exist.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name ANTHROPIC-API-KEY -Force

    Rotates one secret.
#>
[CmdletBinding()]
param(
    # Hardcoded like the database script's defaults, and safe for the same
    # reason: the naming convention carries no random suffix, so the dev vault
    # name is deterministic.
    [string]$VaultName = 'kv-marketagent-dev',

    # Hyphenated, because that is what Key Vault allows and what
    # settings.secret() asks for; it maps each to the underscored, uppercased
    # environment variable it prefers over the vault.
    [string[]]$Name = @(
        'ANTHROPIC-API-KEY'
        'ALPACA-API-KEY'
        'ALPACA-SECRET-KEY'
        'RESEND-API-KEY'
    ),

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Native commands report failure by exit code rather than by throwing, and
# PowerShell ignores that unless asked — still off by default in 7.6.
$PSNativeCommandUseErrorActionPreference = $true

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'az not found. Install the Azure CLI.'
}

# `Read-Host -AsSecureString` reaches EOF immediately on a redirected stdin and
# PowerShell then ends the script — exit 0, nothing prompted, nothing written,
# and no indication that anything went wrong. Refusing up front is the only way
# that failure is visible.
if ([Console]::IsInputRedirected) {
    throw @'
this script needs a terminal: a masked prompt cannot read from redirected
input, and would silently store nothing. Run it directly, or for a scripted
one-off use az:

    az keyvault secret set --vault-name <vault> --name <NAME> --value <value>
'@
}

# Checked before the first prompt rather than after: discovering a missing
# login or a missing role assignment having already typed four secrets is a
# poor trade.
$PSNativeCommandUseErrorActionPreference = $false
az account show --output none 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "not logged in. Run 'az login'."
}

# `list` needs the same data-plane permission as `set`, so a success here means
# the RBAC assignment has propagated and the vault is reachable.
$existing = az keyvault secret list --vault-name $VaultName --query '[].name' -o tsv 2>&1
if ($LASTEXITCODE -ne 0) {
    throw @"
cannot read secrets in $VaultName. Either the vault does not exist, or the
signed-in principal lacks 'Key Vault Secrets Officer' on it. Grant it with
key_vault_administrator_object_ids in terraform/environments/<env>.tfvars, or
check the vault name with: az keyvault list --query "[].name" -o tsv
"@
}
$PSNativeCommandUseErrorActionPreference = $true

$present = @($existing -split '\r?\n' | Where-Object { $_ })
Write-Host "==> $VaultName holds $($present.Count) secret(s)"

# Advisory only. A mistyped or half-pasted key is a class of error that
# otherwise surfaces as a 401 inside a container at 06:00 UTC, and a warning
# costs nothing. Deliberately not a rejection: these prefixes are Anthropic's
# and Alpaca's to change, not ours to enforce.
$expectedPrefixes = @{
    'ANTHROPIC-API-KEY' = 'sk-ant-'
    'ALPACA-API-KEY'    = 'PK'
}

$set = 0
$skipped = 0

foreach ($secretName in $Name) {
    $alreadySet = $present -contains $secretName

    if ($alreadySet -and -not $Force) {
        $answer = (Read-Host "$secretName is already set. Replace it? [y/N]").Trim()
        if ($answer -notmatch '^y') {
            Write-Host "    skipped $secretName"
            $skipped++
            continue
        }
    }

    $envVar = $secretName.Replace('-', '_').ToUpperInvariant()
    $secure = Read-Host "$secretName (Enter to skip, read as `$$envVar locally)" -AsSecureString

    # NetworkCredential is the tidy cross-platform way back to plaintext; the
    # alternative is a manual unmanaged-memory dance for no benefit here.
    $value = [System.Net.NetworkCredential]::new('', $secure).Password

    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "    skipped $secretName"
        $skipped++
        continue
    }

    if ($expectedPrefixes.ContainsKey($secretName) -and
        -not $value.StartsWith($expectedPrefixes[$secretName])) {
        Write-Warning "$secretName does not start with '$($expectedPrefixes[$secretName])' — storing it anyway, but check for a truncated paste."
    }

    # --value puts the secret in this process's argument list, where anything
    # running as the same user could read it for the lifetime of the call. The
    # alternative, --file, writes it to disk instead, which is worse. In a
    # single-user dev container this is the better of the two.
    az keyvault secret set `
        --vault-name $VaultName --name $secretName --value $value --output none

    Write-Host "    set $secretName ($($value.Length) characters)"
    $set++

    $value = $null
    $secure.Dispose()
}

Write-Host "==> $set set, $skipped skipped"

# Names and timestamps only — `list` never returns values, so this is safe to
# print and is the quickest confirmation that a write landed.
if ($set -gt 0) {
    az keyvault secret list --vault-name $VaultName `
        --query 'sort_by([].{name:name,updated:attributes.updated}, &name)' -o table
}
