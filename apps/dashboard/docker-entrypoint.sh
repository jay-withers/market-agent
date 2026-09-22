#!/bin/sh
# Renders the runtime configuration, then starts nginx.
#
# This is what lets one image serve every environment. The alternative — baking
# VITE_API_URL in at build time — means an image that is only correct in the
# environment it was built for, and a rebuild to move it.
set -eu

: "${API_ORIGIN:=}"
: "${KEY_VAULT_URI:=}"
: "${AZURE_CLIENT_ID:=}"
: "${DASHBOARD_PASSCODE:=}"
: "${API_TOKEN:=}"
: "${IDENTITY_ENDPOINT:=}"
: "${IDENTITY_HEADER:=}"
# Not a secret, just unset in a local `docker compose` run — "dev" there beats
# an empty string, which would render as a blank version badge.
: "${IMAGE_TAG:=dev}"

mkdir -p /tmp/dashboard

# DASHBOARD_PASSCODE and API_TOKEN are fetched here, live, with the container's
# managed identity — the same way every other secret in this repo is read —
# rather than through Container Apps' native Key Vault secret reference.
# That mechanism resolves a secret's value once, at revision creation, and
# caches it for every replica and every restart of that revision: rotating
# either secret then needed a brand-new revision to actually take effect,
# which cost a working session to track down. Fetching them ourselves means a
# plain restart — or the next scale-to-zero cold start, which already happens
# routinely at min_replicas=0 — is enough.
#
# KEY_VAULT_URI absent is what keeps `docker compose up` working with no Azure
# at all: DASHBOARD_PASSCODE and API_TOKEN are then whatever was set directly
# in the environment (empty by default, matching every other opt-in secret
# here) rather than fetched — `DASHBOARD_PASSCODE=... make demo` still works.
# IDENTITY_ENDPOINT and IDENTITY_HEADER are injected by Container Apps itself
# when a managed identity is attached; nothing here sets them.
# Each curl's own exit status is checked before its output ever reaches jq,
# deliberately not `curl ... | jq ...` directly: jq exits 0 on empty input by
# default (it just produces no output), so piping straight into it would let
# a failed curl — Key Vault unreachable, a bad token, IDENTITY_ENDPOINT
# missing — masquerade as a successful empty fetch, and this would fail open
# instead of closed exactly when it matters most.
key_vault_token=""
fetch_secret() {
  name=$1
  if [ -z "$key_vault_token" ]; then
    if [ -z "$IDENTITY_ENDPOINT" ] || [ -z "$IDENTITY_HEADER" ]; then
      echo "dashboard: IDENTITY_ENDPOINT/IDENTITY_HEADER not set — no managed identity attached?" >&2
      return 1
    fi
    token_resp=$(curl -sf --max-time 10 \
      -H "X-IDENTITY-HEADER: ${IDENTITY_HEADER}" \
      "${IDENTITY_ENDPOINT}?resource=https%3A%2F%2Fvault.azure.net&api-version=2019-08-01&client_id=${AZURE_CLIENT_ID}") || return 1
    key_vault_token=$(printf '%s' "$token_resp" | jq -r '.access_token')
    [ -n "$key_vault_token" ] && [ "$key_vault_token" != "null" ] || return 1
  fi
  secret_resp=$(curl -sf --max-time 10 -H "Authorization: Bearer ${key_vault_token}" \
    "${KEY_VAULT_URI%/}/secrets/${name}?api-version=7.4") || return 1
  value=$(printf '%s' "$secret_resp" | jq -r '.value')
  [ -n "$value" ] && [ "$value" != "null" ] || return 1
  printf '%s' "$value"
}

if [ -n "$KEY_VAULT_URI" ]; then
  if ! DASHBOARD_PASSCODE=$(fetch_secret DASHBOARD-PASSCODE); then
    echo "dashboard: failed to fetch DASHBOARD-PASSCODE from Key Vault — refusing to start" >&2
    exit 1
  fi
  if ! API_TOKEN=$(fetch_secret API-BEARER-TOKEN); then
    echo "dashboard: failed to fetch API-BEARER-TOKEN from Key Vault — refusing to start" >&2
    exit 1
  fi
fi
export API_ORIGIN API_TOKEN IMAGE_TAG

# envsubst with an explicit variable list: without it, every $-sign in the
# template is substituted, which quietly empties anything that looks like a
# shell variable.
# Rendered into /tmp rather than the served root: the unprivileged nginx image
# runs as uid 101 and /usr/share/nginx/html is root-owned, so writing there
# fails with "Permission denied" at start-up. Keeping the document root
# read-only is the better posture anyway — nginx aliases this one path.
# shellcheck disable=SC2016  # the literal ${API_ORIGIN}/${API_TOKEN}/${IMAGE_TAG}
# *are* the argument: envsubst takes the variable list unexpanded, and
# expanding it here would substitute the values into the list and leave the
# template untouched.
envsubst '${API_ORIGIN} ${API_TOKEN} ${IMAGE_TAG}' \
  < /usr/share/nginx/html/config.json.template \
  > /tmp/dashboard/config.json

echo "dashboard: API_ORIGIN=${API_ORIGIN:-(same origin)}"
echo "dashboard: IMAGE_TAG=${IMAGE_TAG}"
# Presence only, never the value: this line reaches Log Analytics, and
# `${API_TOKEN:-(not set)}` alone would have printed the token itself once
# set, since `:-` only substitutes when the variable is unset or empty.
if [ -n "$API_TOKEN" ]; then
  echo "dashboard: API_TOKEN=(set)"
else
  echo "dashboard: API_TOKEN=(not set)"
fi

# A passcode, not a username and a password: one field to type on a phone, and
# nothing a browser will offer to save as an account. Opt-in exactly as the
# Basic Auth it replaces was — absent means open, which is what keeps `docker
# compose up` serving the dashboard with no credential configured.
#
# nginx compares the cookie verbatim, because this image has no application
# code to verify anything: it is static files and nginx. That also rules out
# gym-log's signed-cookie approach, which works there only because gym-log is
# a Python app that can HMAC an issue time. The cookie therefore *is* the
# passcode, which is the same exposure Basic Auth had — that credential also
# travelled on every single request, just base64'd rather than in a cookie.
#
# A 401 rather than a redirect, with login.html as the error page: the browser
# keeps the URL it asked for, so a deep link into /holdings survives the login
# and lands where it was going. A redirect to /login would have to carry the
# original path and put it back afterwards.
if [ -n "$DASHBOARD_PASSCODE" ]; then
  # Rejected rather than mangled. The passcode rides in a cookie, where a
  # semicolon or a comma ends the value, and it is interpolated into an nginx
  # string, where a quote or a backslash would end that. Set-KeyVaultSecrets.ps1
  # generates from an alphabet that cannot produce any of them, so this only
  # fires on a hand-set value — and failing closed beats a gate that silently
  # compares against a truncated passcode nobody can type.
  case $DASHBOARD_PASSCODE in
    *[\;\,\"\\\ ]*)
      echo "dashboard: DASHBOARD_PASSCODE contains a space, quote, backslash, comma or semicolon — refusing to start" >&2
      exit 1
      ;;
  esac

  # A non-ASCII character (a curly quote, a pound sign, anything pasted from
  # somewhere that "smartened" a character) passes the check above untouched,
  # but login.html's `encodeURIComponent` percent-encodes it into the cookie
  # while this file substitutes it in raw — the two can then never compare
  # equal, and no amount of retyping it correctly fixes that. Caught this the
  # hard way once already.
  if printf '%s' "$DASHBOARD_PASSCODE" | LC_ALL=C grep -q '[^ -~]'; then
    echo "dashboard: DASHBOARD_PASSCODE contains a non-ASCII character — refusing to start" >&2
    exit 1
  fi

  cat > /tmp/dashboard/auth.conf <<CONF
if (\$cookie_ma_passcode != "${DASHBOARD_PASSCODE}") {
    return 401;
}
CONF
  echo "dashboard: passcode gate enabled"
else
  : > /tmp/dashboard/auth.conf
  echo "dashboard: passcode gate disabled (DASHBOARD_PASSCODE not set)"
fi

exec "$@"
