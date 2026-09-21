# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Container Apps runs linux/amd64 only, and the dev host here is arm64 (Docker
# Desktop on Apple Silicon). A native build produces an image that crash-loops
# with an exec format error and no other clue, so every build for deployment
# must pass --platform linux/amd64. The Makefile's `build` target does it rather
# than leaving it to memory.

# Builder and runtime both derive from `base`, and that is load-bearing rather
# than tidy. The runtime stage copies the whole virtualenv, and a venv is bound
# to the exact minor version that created it — its packages live in
# lib/python3.X/site-packages and no other interpreter will look there. While
# the stages named their Python separately, a Renovate "non-major" bump moved
# the runtime from 3.12 to 3.14 and left the builder's uv image on 3.12; the
# image that produced had no site-packages on sys.path at all and died with a
# bare "No module named 'marketagent'". One FROM line for the version means the
# two cannot drift apart again.
FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS base


FROM base AS builder

# uv is copied in rather than supplying the base image, which is exactly what
# lets the builder share the runtime's interpreter. UV_PYTHON_DOWNLOADS=never
# then stops uv fetching a different one of its own.
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source, so editing a module does not re-resolve the
# whole environment. --frozen fails rather than silently updating uv.lock,
# which is what makes the image reproducible.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable

COPY src ./src
# --no-editable matters: without it uv installs the project as a link back to
# /app/src, and the runtime stage copies only the virtualenv — leaving a .pth
# that points at a directory which does not exist there. The failure is a bare
# "No module named 'marketagent'" from an image whose venv looks complete.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM base AS runtime

# This label is what connects the pushed package to this repository, and the
# connection is what gives a workflow's GITHUB_TOKEN write access to it. Both
# packages were first pushed by hand without it, so they were user-scoped and
# unlinked, and cd-publish failed with `denied: permission_denied:
# write_package` however its `packages: write` permission was declared.
LABEL org.opencontainers.image.source="https://github.com/jay-withers/market-agent"

# psycopg is installed as the binary wheel, so no libpq or build toolchain is
# needed at runtime — which is most of why this image stays small.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root, and no shell: nothing in here needs to log in, and the API is
# publicly reachable. The uid is numeric and USER names the number, not the
# name — a runtime enforcing runAsNonRoot has to resolve the user before the
# container starts, and it cannot read this image's /etc/passwd to do it.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin marketagent

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

USER 10001

EXPOSE 8000

# The probe target. Kept in the image so `docker run` and Compose can use the
# same check Container Apps does.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

# One image, five entrypoints: Terraform sets `command = ["marketagent"]` and
# picks the workload with `args`.
ENTRYPOINT ["marketagent"]
CMD ["api"]
