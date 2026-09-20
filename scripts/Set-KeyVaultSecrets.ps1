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

    Not everything here is a secret. ANTHROPIC-CREDIT-USD is the API credit the
    experiment started with, in dollars — Anthropic publishes no balance
    endpoint, so the daily summary's runway figure can only be that number minus
    the spend the database recorded. SUMMARY-EMAIL-TO is the daily summary's
    recipient, and it lives in Key Vault for a different reason: this repository
    is public and so are terraform/environments/*.tfvars, so an address in
    either would be committed permanently. Reading it at runtime also means
    changing the recipient needs no redeploy. It is prompted **unmasked**,
    because a masked prompt hides a typo and a mistyped recipient sends the
    summary nowhere — or to a stranger.

    DASHBOARD-PASSCODE gates the dashboard, and is the one secret here this
    script will **generate** for you: press Enter at its prompt and it writes
    a random 12-character passcode and prints it once. Nothing else in this
    repo generates a secret — in particular Terraform does not, because it
    creates no `azurerm_key_vault_secret` at all and a generated one would
    live in Terraform state.

    It is also read differently from the rest: Terraform wires it into the
    dashboard container app as a native Key Vault secret reference
    (main.container-apps.tf), because the dashboard is static nginx with no
    application code to call Key Vault itself. That means — unlike every other
    secret this script sets — it must exist **before** the `terraform apply`
    that first adds that secret block, or the dashboard revision fails to
    resolve it. Set it first on a fresh environment.

    It replaced a DASHBOARD-USERNAME/DASHBOARD-PASSWORD pair: the browser's
    Basic Auth dialog always asks for a username, there was only ever one
    account, and a passcode is one field to type on a phone. Delete the two
    old secrets once the new revision is up.

    API-BEARER-TOKEN is the one secret two apps share. The API reads it lazily
    via settings.secret(), same as every other application secret, and gates
    every /api/* route behind it once API_REQUIRE_TOKEN is set — but the
    dashboard needs the same value to call the API on the browser's behalf, so
    it is *also* wired into the dashboard container as a Key Vault secret
    reference, the same way as DASHBOARD-PASSCODE above, and carries
    the same before-apply requirement on a fresh environment. Generate it as a
    plain random string (`openssl rand -hex 32` or similar) rather than typing
    one: it is embedded verbatim inside a JSON string in the dashboard's
    rendered config.json, and a quote or backslash in it would break that
    file.

    Existing secrets are left alone unless you say otherwise, because Key Vault
    versions every write and re-setting an unchanged value just adds noise.

    Requires az on PATH, a completed `az login`, and `Key Vault Secrets
    Officer` on the vault — which whoever ran `terraform apply` already has.

.PARAMETER Name
    Which values to prompt for. Defaults to all eight the application uses.

.PARAMETER Force
    Overwrite an existing secret without asking.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1

    Prompts for each of the eight, skipping any that already exist.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name ANTHROPIC-API-KEY -Force

    Rotates one secret.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name SUMMARY-EMAIL-TO

    Switches the daily summary email on, or points it somewhere else. Takes
    effect on the next scheduled run — no redeploy, because the value is read
    at runtime.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name DASHBOARD-PASSCODE -Force

    Rotates the dashboard passcode. Press Enter to have one generated; it is
    printed once. Every browser holding the old one is logged out on the next
    revision, which is the intended way to revoke access.

.EXAMPLE
    ./scripts/Set-KeyVaultSecrets.ps1 -Name API-BEARER-TOKEN -Force

    Rotates the token both the API and the dashboard use. Takes effect for the
    API on its next revision (API_REQUIRE_TOKEN is set separately in
    Terraform); the dashboard also needs a `terraform apply` to pick it up,
    same as DASHBOARD-PASSCODE.
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
        'ANTHROPIC-CREDIT-USD'
        'DASHBOARD-PASSCODE'
        'API-BEARER-TOKEN'
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
$plainText = @('SUMMARY-EMAIL-TO', 'ANTHROPIC-CREDIT-USD')

# Offered as a generated value rather than prompted for blind. A passcode
# nobody chose is a passcode nobody reuses from somewhere else, and there is
# no reason for a human to invent one.
$generated = @('DASHBOARD-PASSCODE')

function New-Passcode {
    <#
    .SYNOPSIS
        A random passcode that is safe to put in a cookie and read aloud.

    .DESCRIPTION
        The alphabet is doing real work in three directions:

        - It excludes `;` `,` `"` `\` and whitespace. The passcode travels as a
          cookie value, where a semicolon or comma ends it, and nginx compares
          it inside a quoted string in a generated config, where a quote or a
          backslash ends that. docker-entrypoint.sh refuses to start on any of
          them; generating from an alphabet that cannot produce one means that
          guard never fires on our own output.
        - It excludes 0/O and 1/I/L, which are the characters someone
          mistypes reading a code off another screen.
        - It is upper case only, so the login field can autocapitalise without
          fighting the person typing.

        30 characters over 12 positions is about 59 bits, which is far more
        than a rate-unlimited single-user gate needs and still short enough to
        type on a phone.

        RandomNumberGenerator.GetInt32 rather than Get-Random: the latter is a
        deterministic PRNG seeded from the clock and is documented as unsuitable
        for anything security-bearing.
    #>
    $alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
    $chars = foreach ($i in 1..12) {
        $alphabet[[System.Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)]
    }
    # Grouped, because a 12-character run is read back wrong far more often
    # than three groups of four. The hyphens are part of the passcode.
    $joined = -join $chars
    "$($joined.Substring(0, 4))-$($joined.Substring(4, 4))-$($joined.Substring(8, 4))"
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
    $isSecret = $plainText -notcontains $secretName
    $secure = $null

    if ($generated -contains $secretName) {
        # Enter generates. Typing one is still allowed — a passcode being read
        # down a phone to someone is a real case, and "MARKET-AGENT-2026" is
        # their risk to take.
        $typed = (Read-Host "$secretName (Enter to generate one, or type your own)").Trim()
        if ($typed) {
            $value = $typed
        }
        else {
            $value = New-Passcode
        }
    }
    elseif ($isSecret) {
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

    # Advisory too. The summary parses this with Decimal() and ignores a value
    # it cannot read, so a typo costs the runway line rather than the email —
    # but it does so silently a day later, which is worth a warning now. A bare
    # number: a currency symbol or thousands separator is what a paste from a
    # billing page carries.
    if ($secretName -eq 'ANTHROPIC-CREDIT-USD') {
        if ($value -notmatch '^[0-9]+(\.[0-9]+)?$') {
            Write-Warning "$secretName should be a plain number of US dollars, e.g. 25 or 25.00 — '$value' will be ignored by the summary if it cannot be parsed."
        }
        Write-Host "    note: this is the credit you started with, not a live balance — Anthropic publishes no balance endpoint, so the summary subtracts its own recorded spend from it."
    }

    # Advisory. This value lands inside a JSON string literal in the
    # dashboard's rendered config.json (docker-entrypoint.sh does no escaping,
    # matching how API_ORIGIN is substituted there already), so a quote or
    # backslash breaks that file rather than the token.
    if ($secretName -eq 'API-BEARER-TOKEN' -and $value -match '["\\]') {
        Write-Warning "$secretName contains a quote or backslash, which will break the dashboard's config.json — use a plain alphanumeric token, e.g. from 'openssl rand -hex 32'."
    }

    # A refusal, not a warning, and the only one in this script. The dashboard
    # entrypoint exits non-zero on any of these characters, so storing one
    # would not produce a weak gate — it would produce a container that will
    # not start, discovered as a failed revision some time after this script
    # said it had succeeded. A generated passcode can never contain one; this
    # only ever fires on a hand-typed value.
    if ($secretName -eq 'DASHBOARD-PASSCODE' -and $value -match '[;,"\\\s]') {
        Write-Warning "$secretName cannot contain a space, quote, backslash, comma or semicolon — the cookie and the nginx config both end at one, and the dashboard refuses to start. Not stored."
        $skipped++
        continue
    }

    # --value puts the secret in this process's argument list, where anything
    # running as the same user could read it for the lifetime of the call. The
    # alternative, --file, writes it to disk instead, which is worse. In a
    # single-user dev container this is the better of the two.
    az keyvault secret set `
        --vault-name $VaultName --name $secretName --value $value --output none

    # The value itself for a non-secret, which is the only way to spot a typo;
    # a length only, for a secret.
    if ($generated -contains $secretName) {
        # Shown in full, deliberately, and it is the one secret here that must
        # be: it was generated a moment ago, nobody has seen it, and Key Vault
        # is the only other copy. Printed on its own lines because this is the
        # one line in the run worth copying somewhere.
        Write-Host ''
        Write-Host "    $secretName is:  $value"
        Write-Host '    Write it down now — this is the only time it is displayed.'
        Write-Host '    The dashboard picks it up on its next revision:'
        Write-Host '      make deploy IMAGE_TAG=<tag>   (or `az containerapp revision restart`)'
        Write-Host ''
    }
    elseif ($isSecret) {
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
