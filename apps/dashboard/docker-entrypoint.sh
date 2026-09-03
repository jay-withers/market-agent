#!/bin/sh
# Renders the runtime configuration, then starts nginx.
#
# This is what lets one image serve every environment. The alternative — baking
# VITE_API_URL in at build time — means an image that is only correct in the
# environment it was built for, and a rebuild to move it.
set -eu

: "${API_ORIGIN:=}"

# envsubst with an explicit variable list: without it, every $-sign in the
# template is substituted, which quietly empties anything that looks like a
# shell variable.
# Rendered into /tmp rather than the served root: the unprivileged nginx image
# runs as uid 101 and /usr/share/nginx/html is root-owned, so writing there
# fails with "Permission denied" at start-up. Keeping the document root
# read-only is the better posture anyway — nginx aliases this one path.
mkdir -p /tmp/dashboard
# shellcheck disable=SC2016  # the literal ${API_ORIGIN} *is* the argument:
# envsubst takes the variable list unexpanded, and expanding it here would
# substitute the value into the list and leave the template untouched.
envsubst '${API_ORIGIN}' \
  < /usr/share/nginx/html/config.json.template \
  > /tmp/dashboard/config.json

echo "dashboard: API_ORIGIN=${API_ORIGIN:-(same origin)}"

exec "$@"
