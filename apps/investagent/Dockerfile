# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# Container Apps runs linux/amd64 only, and the dev host here is arm64 (Docker
# Desktop on Apple Silicon). A native build produces an image that crash-loops
# with an exec format error and no other clue, so every build for deployment
# must pass --platform linux/amd64. The Makefile's `build` target does it rather
# than leaving it to memory.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

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
# "No module named 'investagent'" from an image whose venv looks complete.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

# psycopg is installed as the binary wheel, so no libpq or build toolchain is
# needed at runtime — which is most of why this image stays small.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root, and no shell: nothing in here needs to log in, and the API is
# publicly reachable. The uid is numeric and USER names the number, not the
# name — a runtime enforcing runAsNonRoot has to resolve the user before the
# container starts, and it cannot read this image's /etc/passwd to do it.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin investagent

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

USER 10001

EXPOSE 8000

# The probe target. Kept in the image so `docker run` and Compose can use the
# same check Container Apps does.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

# One image, three entrypoints: Terraform sets `command = ["investagent"]` and
# picks the workload with `args`.
ENTRYPOINT ["investagent"]
CMD ["api"]
