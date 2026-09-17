#!/bin/sh
# Renders the runtime configuration, then starts nginx.
#
# This is what lets one image serve every environment. The alternative — baking
# VITE_API_URL in at build time — means an image that is only correct in the
# environment it was built for, and a rebuild to move it.
set -eu

: "${API_ORIGIN:=}"
: "${API_TOKEN:=}"
: "${DASHBOARD_USERNAME:=}"
: "${DASHBOARD_PASSWORD:=}"

# envsubst with an explicit variable list: without it, every $-sign in the
# template is substituted, which quietly empties anything that looks like a
# shell variable.
# Rendered into /tmp rather than the served root: the unprivileged nginx image
# runs as uid 101 and /usr/share/nginx/html is root-owned, so writing there
# fails with "Permission denied" at start-up. Keeping the document root
# read-only is the better posture anyway — nginx aliases this one path.
mkdir -p /tmp/dashboard
# shellcheck disable=SC2016  # the literal ${API_ORIGIN}/${API_TOKEN} *are* the
# argument: envsubst takes the variable list unexpanded, and expanding it here
# would substitute the values into the list and leave the template untouched.
envsubst '${API_ORIGIN} ${API_TOKEN}' \
  < /usr/share/nginx/html/config.json.template \
  > /tmp/dashboard/config.json

echo "dashboard: API_ORIGIN=${API_ORIGIN:-(same origin)}"
# Presence only, never the value: this line reaches Log Analytics, and
# `${API_TOKEN:-(not set)}` alone would have printed the token itself once
# set, since `:-` only substitutes when the variable is unset or empty.
if [ -n "$API_TOKEN" ]; then
  echo "dashboard: API_TOKEN=(set)"
else
  echo "dashboard: API_TOKEN=(not set)"
fi

# Basic Auth, opt-in: absent means open, which is what keeps `docker compose
# up` serving the dashboard with no credential configured. There is no
# htpasswd file and no crypt() hash — nginx's Alpine base is musl, whose
# crypt() support for the $apr1$/$1$ formats varies by version, so hashing a
# password portably would need openssl or htpasswd, neither of which the image
# carries. Comparing the whole "Basic <base64>" header verbatim needs only
# base64, which busybox always provides, and is exactly as strong as Basic
# Auth ever is — the credential travels the wire as this same base64, not a
# hash, so a hashed comparison would buy nothing here anyway.
if [ -n "$DASHBOARD_USERNAME" ] && [ -n "$DASHBOARD_PASSWORD" ]; then
  credential=$(printf '%s:%s' "$DASHBOARD_USERNAME" "$DASHBOARD_PASSWORD" | base64 -w0)
  cat > /tmp/dashboard/auth.conf <<CONF
if (\$http_authorization != "Basic ${credential}") {
    return 401;
}
add_header WWW-Authenticate 'Basic realm="dashboard"' always;
CONF
  echo "dashboard: basic auth enabled"
else
  : > /tmp/dashboard/auth.conf
  echo "dashboard: basic auth disabled (DASHBOARD_USERNAME/DASHBOARD_PASSWORD not set)"
fi

exec "$@"
