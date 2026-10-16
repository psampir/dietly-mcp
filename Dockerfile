# syntax=docker/dockerfile:1
#
# dietly-mcp — unofficial Dietly MCP server (Streamable HTTP + stdio).
#
# Two stages: the first resolves the committed lock file with uv into a venv, the
# second ships that venv on a slim interpreter with no build tooling, no source
# tree and no secrets. The runtime image listens on 0.0.0.0:8787 and serves
# MCP at /mcp plus a public /healthz — which is why the defaults here go together
# with MCP_AUTH_TOKEN (see docs/deploy.md; a non-loopback bind without a token is
# refused at start-up).
#
#   podman build -t dietly-mcp:dev .
#   podman run --rm -p 127.0.0.1:8787:8787 -e MCP_AUTH_TOKEN=... dietly-mcp:dev
#
# python:3.13-slim (not 3.14) is the supported floor for the committed lock, whose
# dependency resolution is universal for 3.12+ / 3.14+ (see uv.lock markers).

# --------------------------------------------------------------------------- #
# Stage 1 — resolve dependencies and install the project with uv
# --------------------------------------------------------------------------- #
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first: this layer only changes when the lock file does.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself. --no-editable installs a real wheel, so the runtime
# stage needs neither the source tree nor hatchling.
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable \
 && /app/.venv/bin/dietly-mcp-server --version \
 && /app/.venv/bin/python -c "import dietly_mcp, mcp, starlette, uvicorn"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="dietly-mcp" \
      org.opencontainers.image.description="Unofficial Dietly (dietly.pl) MCP server — read tools plus dry-run-guarded, audited write tools, over Streamable HTTP or stdio." \
      org.opencontainers.image.source="https://github.com/psampir/dietly-mcp" \
      org.opencontainers.image.url="https://github.com/psampir/dietly-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.documentation="https://github.com/psampir/dietly-mcp/blob/main/docs/deploy.md"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DIETLY_TRANSPORT=streamable-http \
    DIETLY_HTTP_HOST=0.0.0.0 \
    DIETLY_HTTP_PORT=8787 \
    DIETLY_STATE_DIR=/data \
    DIETLY_CONFIG_DIR=/config

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

# Non-root, with writable state (/data: session cookie jar + audit log) and a
# read-only home for a mounted /config/credentials.env.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dietly \
 && install -d -o 10001 -g 10001 -m 0700 /data \
 && install -d -o 10001 -g 10001 -m 0700 /config

USER 10001
WORKDIR /app

VOLUME ["/data"]
EXPOSE 8787

# /healthz never needs credentials, so the probe passes before the first login.
# (Override DIETLY_HTTP_PORT and this follows; the check is skipped for stdio.)
# Note: podman's default OCI output drops HEALTHCHECK — build with
# `podman build --format docker` (or repeat the command in the quadlet unit).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('DIETLY_HTTP_PORT','8787')+'/healthz', timeout=3).read()" || exit 1

# stdio stays available: `podman run -i --rm -e DIETLY_TRANSPORT=stdio <image>`
ENTRYPOINT ["dietly-mcp-server"]
