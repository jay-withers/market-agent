#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Prompts for the application's Key Vault values and stores them.

.DESCRIPTION
    Terraform owns the vault and its RBAC but deliberately owns none of the
    values, so populating them is a manual step. This is that step, made
    repeatable.

    Secrets are read from a masked prompt, held only as a SecureString until the
    moment they are passed to `az`, and never written to disk, logged, echoed,
    or placed in shell history. Press Enter at a prompt to skip one — they are
    needed at different points (Anthropic and the two Alpaca keys for the
    agent, Resend only for the daily summary), so there is no need to have them
    all to hand.

    Not everything here is a secret. SUMMARY-EMAIL-TO is the daily summary's
    recipient, and it lives in Key Vault for a different reason: this repository
    is public and so are terraform/environments/*.tfvars, so an address in
    either would be committed permanently. Reading it at runtime also means
    changing the recipient needs no redeploy. It is prompted **unmasked**,
    because a masked prompt hides a typo and a mistyped recipient sends the
    summary nowhere — or to a stranger.

    Existing secrets are left alone unless you say otherwise, because Key Vault
    versions every write and re-setting an unchanged value just adds noise.

    Requires az on PATH, a completed `az login`, and `Key Vault Secrets
    Officer` on the vault — which whoever ran `terraform apply` already has.

.PARAMETER Name
    Which values to prompt for. Defaults to all five the application uses.

.PARAMETER Force
    Overwrite an existing secret without asking.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1

    Prompts for each of the five, skipping any that already exist.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name ANTHROPIC-API-KEY -Force

    Rotates one secret.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name SUMMARY-EMAIL-TO

    Switches the daily summary email on, or points it somewhere else. Takes
    effect on the next scheduled run — no redeploy, because the value is read
    at runtime.
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
        'SUMMARY-EMAIL-TO'
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

# Prompted in the clear, and echoed back on success. These are configuration
# that happens to live in Key Vault rather than credentials, and hiding them
# only hides mistakes: a mistyped API key fails loudly on first use, whereas a
# mistyped email address silently delivers nowhere.
$plainText = @('SUMMARY-EMAIL-TO')

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
    $isSecret = $plainText -notcontains $secretName
    $secure = $null

    if ($isSecret) {
        $secure = Read-Host "$secretName (Enter to skip, read as `$$envVar locally)" -AsSecureString

        # NetworkCredential is the tidy cross-platform way back to plaintext;
        # the alternative is a manual unmanaged-memory dance for no benefit.
        $value = [System.Net.NetworkCredential]::new('', $secure).Password
    }
    else {
        $value = (Read-Host "$secretName (Enter to skip, read as `$$envVar locally)").Trim()
    }

    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "    skipped $secretName"
        $skipped++
        continue
    }

    if ($expectedPrefixes.ContainsKey($secretName) -and
        -not $value.StartsWith($expectedPrefixes[$secretName])) {
        Write-Warning "$secretName does not start with '$($expectedPrefixes[$secretName])' — storing it anyway, but check for a truncated paste."
    }

    # Advisory, like the prefix checks: the recipient may be a comma-separated
    # list, and every entry should look like an address. Storing a malformed one
    # costs a silent non-delivery rather than an error.
    #
    # The character classes are printable ASCII either side of '@' rather than
    # the obvious [^@\s], because Resend rejects a non-ASCII `to` with a 422 and
    # the way that happens is invisible: an address pasted from somewhere that
    # autocorrects quotes arrives wrapped in U+2018/U+2019, which a
    # "not @ and not whitespace" class accepts happily. It has already cost one
    # day's summary email.
    if ($secretName -eq 'SUMMARY-EMAIL-TO') {
        $addressPattern = '^[\x21-\x3f\x41-\x7e]+@[\x21-\x3f\x41-\x7e]+\.[\x21-\x3f\x41-\x7e]+$'
        $bad = @($value -split ',' | ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and $_ -notmatch $addressPattern })
        if ($bad.Count -gt 0) {
            Write-Warning "$secretName does not look like an email address: $($bad -join ', ')"
        }
        Write-Host "    note: Resend's shared sender only delivers to the address that owns the Resend account, until a domain is verified."
    }

    # --value puts the secret in this process's argument list, where anything
    # running as the same user could read it for the lifetime of the call. The
    # alternative, --file, writes it to disk instead, which is worse. In a
    # single-user dev container this is the better of the two.
    az keyvault secret set `
        --vault-name $VaultName --name $secretName --value $value --output none

    # The value itself for a non-secret, which is the only way to spot a typo;
    # a length only, for a secret.
    if ($isSecret) {
        Write-Host "    set $secretName ($($value.Length) characters)"
    }
    else {
        Write-Host "    set $secretName = $value"
    }
    $set++

    $value = $null
    if ($secure) { $secure.Dispose() }
}

Write-Host "==> $set set, $skipped skipped"

# Names and timestamps only — `list` never returns values, so this is safe to
# print and is the quickest confirmation that a write landed.
if ($set -gt 0) {
    az keyvault secret list --vault-name $VaultName `
        --query 'sort_by([].{name:name,updated:attributes.updated}, &name)' -o table
}
