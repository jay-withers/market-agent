# syntax=docker/dockerfile:1

# Container Apps runs linux/amd64 only and the dev host is arm64, so a build for
# deployment must pass --platform linux/amd64. The Makefile does it rather than
# leaving it to memory.

FROM node:24-alpine AS builder

WORKDIR /app

# Dependencies before source, so editing a component does not reinstall the
# world. `npm ci` fails rather than silently updating the lockfile.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:aa8c9087d36d93e9d650c5365f883b421e8214aedbad24ade52b844c583358f1 AS runtime

# The unprivileged image already runs as uid 101 and listens above 1024, which
# is what Container Apps wants; rolling our own non-root nginx means fighting
# its pid file, cache and log paths.
USER 101

COPY --chown=101:101 nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder --chown=101:101 /app/dist /usr/share/nginx/html
# Copied separately: Vite emits it into dist, but keeping the template beside
# the entrypoint makes it obvious that config.json is generated, not shipped.
COPY --chown=101:101 public/config.json.template /usr/share/nginx/html/config.json.template
COPY --chown=101:101 docker-entrypoint.sh /docker-entrypoint.d/40-render-config.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["sh", "-c", "wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1"]
