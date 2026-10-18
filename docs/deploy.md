# Deploying dietly-mcp (Streamable HTTP + container)

`dietly-mcp` is designed to run **long-lived on your own server** and be reached by remote MCP
clients — Open WebUI (native MCP, v0.6.31+) and anything else that speaks **Streamable HTTP**. The
same binary still speaks **stdio** for local use, so nothing about the OpenCode setup changes.

> **Read this first.** The HTTP endpoint is not read-only. `dietly_switch`, `dietly_apply` and
> `dietly_revert` modify your **real Dietly order** (they are dry-run by default and every attempt is
> audited, but the write tools do work over HTTP). Treat the port like a password manager: keep it on
> loopback, or expose it with `MCP_AUTH_TOKEN` set *and* TLS in front of it.

- [1. Image](#1-image)
- [2. Quick start (podman)](#2-quick-start-podman)
- [3. Credentials](#3-credentials)
- [4. The token: set, rotate, revoke](#4-the-token-set-rotate-revoke)
- [5. Endpoints and what is public](#5-endpoints-and-what-is-public)
- [6. docker-compose](#6-docker-compose)
- [7. Podman quadlet (systemd)](#7-podman-quadlet-systemd)
- [8. Behind a reverse proxy](#8-behind-a-reverse-proxy)
- [9. Connecting clients (Open WebUI, others)](#9-connecting-clients-open-webui-others)
- [10. Updating](#10-updating)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Publishing a release (maintainer)](#12-publishing-a-release-maintainer)

---

## 1. Image

Multi-arch images are published to GHCR on every `v*` tag by
[`.github/workflows/release.yml`](../.github/workflows/release.yml):

```
ghcr.io/psampir/dietly-mcp:latest        # newest final release
ghcr.io/psampir/dietly-mcp:0.2.0         # exact version
ghcr.io/psampir/dietly-mcp:0.2           # newest patch of 0.2
```

Built for `linux/amd64` and `linux/arm64`, with build provenance and an SBOM attached
(`docker buildx imagetools inspect ghcr.io/psampir/dietly-mcp:0.2.0`).

> **One-time step after the very first push:** the GHCR package is **private by default**. Open
> `https://github.com/users/psampir/packages/container/dietly-mcp/settings` and set *Package
> visibility* → **Public** (then `podman pull` needs no login). Alternatively keep it private and
> `podman login ghcr.io` with a PAT that has `read:packages`.

Runtime properties: non-root (`uid 10001`), Python 3.13 slim, no build tooling, no source tree, no
secrets in any layer (`.dockerignore` keeps credentials/sessions/venvs out of the context).

## 2. Quick start (podman)

```fish
# a token is mandatory for anything but a loopback bind
set -gx MCP_AUTH_TOKEN (openssl rand -hex 32)

podman run -d --name dietly-mcp \
  --restart=unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -e MCP_AUTH_TOKEN=$MCP_AUTH_TOKEN \
  -e DIETLY_EMAIL=you@example.com \
  -e DIETLY_PASSWORD=<your-password> \
  -v dietly-data:/data \
  ghcr.io/psampir/dietly-mcp:0.2.0

curl -s http://127.0.0.1:8787/healthz
```

Building locally instead (the image defaults are the same ones baked in):

```fish
podman build --format docker -t dietly-mcp:dev .   # --format docker keeps HEALTHCHECK
podman run --rm -p 127.0.0.1:8787:8787 -e MCP_AUTH_TOKEN=(openssl rand -hex 32) dietly-mcp:dev
```

What the container sets for you: `DIETLY_TRANSPORT=streamable-http`, `DIETLY_HTTP_HOST=0.0.0.0`,
`DIETLY_HTTP_PORT=8787`, `DIETLY_STATE_DIR=/data`, `DIETLY_CONFIG_DIR=/config`. To run the image
over stdio instead (e.g. from a local client), override the transport and keep stdin open:

```fish
podman run -i --rm -e DIETLY_TRANSPORT=stdio -v dietly-data:/data ghcr.io/psampir/dietly-mcp:0.2.0
```

## 3. Credentials

Two equivalent ways — **never** bake them into an image, and never commit them:

```fish
# (a) environment
-e DIETLY_EMAIL=you@example.com -e DIETLY_PASSWORD=<your-password>

# (b) a mounted file — the container image already points at /config
mkdir -p ~/.config/dietly-mcp
printf '%s\n' 'DIETLY_EMAIL=you@example.com' 'DIETLY_PASSWORD=<your-password>' \
  > ~/.config/dietly-mcp/credentials.env
chmod 600 ~/.config/dietly-mcp/credentials.env
# then: -v ~/.config/dietly-mcp:/config:ro
```

The server **starts without credentials** and keeps serving `/healthz` — `credentialsConfigured`
tells you whether it found them. Tools then answer with an actionable error
(`{"ok": false, "error": ..., "hint": ...}`) instead of failing the whole server, which is what makes
container orchestration and smoke tests possible.

The session cookie jar (`/data/session.json`) and the write audit log (`/data/audit.jsonl`) live in
the `/data` volume, so **mount `/data`** or you lose the login session (and its audit trail) on every
recreate.

## 4. The token: set, rotate, revoke

| Bind | `MCP_AUTH_TOKEN` | Behaviour |
|---|---|---|
| `127.0.0.1` (default) | unset | allowed — loopback only, no other machine can reach it |
| `127.0.0.1` | set | bearer required as well (harmless) |
| `0.0.0.0`, `::`, LAN IP, host name | unset | **refused at start-up** with a clear message |
| `0.0.0.0`, `::`, LAN IP, host name | set | bearer required |

Clients must then send `Authorization: Bearer <token>` on every MCP request; a request without it (or
with a wrong token) is answered `401` **before any tool runs** — no Dietly call, no order change.

```fish
openssl rand -hex 32       # generate  (any long random string works; treat it as a password)
```

Rotate it:

```fish
# 1. generate a new token
set new (openssl rand -hex 32)
# 2. recreate the container with it (quadlet: edit + daemon-reload + restart)
podman rm -f dietly-mcp && podman run -d ... -e MCP_AUTH_TOKEN=$new ...
# 3. update every client that holds the old token (Open WebUI's MCP server header, your scripts, ...)
```

Rotation is a restart, and restarts are cheap: the login session lives on the `/data` volume, so the
server does **not** have to log in again. To revoke access without touching Dietly, just remove the
token and re-bind to `127.0.0.1` (or stop the container). Remember that a leaked token is enough to
change your order — rotate it **and** check `/data/audit.jsonl`.

Tokens are never logged: the start-up banner says *whether* auth is required, never what it is.

## 5. Endpoints and what is public

| Path | Auth | Notes |
|---|---|---|
| `POST /mcp` | bearer (when `MCP_AUTH_TOKEN` is set) | the MCP Streamable-HTTP endpoint: `initialize`, `tools/list`, `tools/call`. Sessions use the `Mcp-Session-Id` header (`POST`/`GET`/`DELETE` all live here) |
| `GET /healthz` | **none** | liveness/readiness JSON, always safe to poll |

```json
{
  "status": "ok",
  "service": "dietly-mcp",
  "version": "0.2.0",
  "transport": "streamable-http",
  "mcpPath": "/mcp",
  "uptimeSeconds": 12.34,
  "authRequired": true,
  "credentialsConfigured": true,
  "sessionConfigured": false
}
```

That is the whole payload — by construction. No email, no token, no cookie, no order data, no paths.
`credentialsConfigured` / `sessionConfigured` are booleans, so this is safe to expose to a monitoring
system or an unauthenticated health probe.

## 6. docker-compose

```yaml
# docker-compose.yml (docker compose up -d)
services:
  dietly-mcp:
    image: ghcr.io/psampir/dietly-mcp:0.2.0
    container_name: dietly-mcp
    restart: unless-stopped
    ports:
      # loopback only: reach it from the host, or put a reverse proxy in front
      - "127.0.0.1:8787:8787"
    environment:
      MCP_AUTH_TOKEN: ${MCP_AUTH_TOKEN:?set MCP_AUTH_TOKEN in .env}
      # Credentials can also come from /config/credentials.env (see volumes below).
      DIETLY_EMAIL: ${DIETLY_EMAIL}
      DIETLY_PASSWORD: ${DIETLY_PASSWORD}
    volumes:
      - dietly-data:/data                 # session cookie jar + audit log
      # - ./credentials.env:/config/credentials.env:ro   # alternative to the env vars
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  dietly-data:
```

With a `.env` file next to it (`chmod 600 .env`) holding `MCP_AUTH_TOKEN`, `DIETLY_EMAIL` and
`DIETLY_PASSWORD`.

## 7. Podman quadlet (systemd)

Quadlet turns a container into a normal systemd service — no `podman run` on boot, automatic restart,
`systemctl status` and journald logs. Save as
`~/.config/containers/systemd/dietly-mcp.container` (rootless; use
`/etc/containers/systemd/` for a system service) and put the secrets in an `EnvironmentFile` that is
**only** readable by you.

```ini
# ~/.config/containers/systemd/dietly-mcp.container
[Unit]
Description=Dietly MCP server (unofficial; Streamable HTTP)
Documentation=https://github.com/psampir/dietly-mcp/blob/main/docs/deploy.md
Wants=network-online.target
After=network-online.target

[Container]
Image=ghcr.io/psampir/dietly-mcp:0.2.0
ContainerName=dietly-mcp

# Never expose a non-loopback bind without the token: keep these two together.
Environment=MCP_AUTH_TOKEN=change-me-to-a-32-byte-random-string
Environment=DIETLY_TRANSPORT=streamable-http
Environment=DIETLY_HTTP_HOST=0.0.0.0
Environment=DIETLY_HTTP_PORT=8787
Environment=DIETLY_STATE_DIR=/data
Environment=DIETLY_CONFIG_DIR=/config
PublishPort=127.0.0.1:8787:8787

# Credentials: either the two variables above in an env file ...
# EnvironmentFile=%h/.config/dietly-mcp/dietly-mcp.env
# ... or a read-only credentials file on the host:
Volume=%h/.config/dietly-mcp/credentials.env:/config/credentials.env:ro
# Session cookie jar + audit log survive restarts and updates.
Volume=dietly-data:/data

# Built from a Dockerfile HEALTHCHECK; repeat it here so it also works when the
# image was built with the default OCI format (podman drops HEALTHCHECK there).
HealthCmd=python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=3)"
HealthInterval=30s
HealthTimeout=5s
HealthRetries=3
HealthStartPeriod=10s
HealthOnFailure=none

[Service]
Restart=on-failure
TimeoutStartSec=120
# rootless + no privileges: the image is already non-root, keep it that way
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

```fish
systemctl --user daemon-reload
systemctl --user start dietly-mcp
systemctl --user status dietly-mcp        # health: "healthy"
journalctl --user -u dietly-mcp -f        # logs (stdout of the container)
podman volume inspect dietly-data          # where session.json / audit.jsonl live
```

## 8. Behind a reverse proxy

Any TLS-terminating proxy works. Three things matter: **forward the `Authorization` header**, keep the
**`Mcp-Session-Id`** header (default in every proxy), and do **not** buffer/limit the event-stream
responses (`/mcp` may answer with `text/event-stream`). Long-lived streams mean read timeouts should be
generous and proxy buffering disabled for that location.

Caddy (`Caddyfile`), which gets TLS for free:

```caddyfile
dietly.example.com {
    # SSE/streaming: no buffering, no short read timeout
    reverse_proxy 127.0.0.1:8787 {
        flush_interval -1
    }
    header {
        # only you should be able to reach it
        X-Content-Type-Options nosniff
    }
}
```

nginx:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;   # the bearer token
    proxy_set_header Connection "";
    proxy_buffering off;          # SSE
    proxy_read_timeout 1h;        # long-lived streams
}
location /healthz {
    proxy_pass http://127.0.0.1:8787;
}
```

Notes:

- The container binds `0.0.0.0` so a sibling container/proxy can reach it; publishing it as
  `-p 127.0.0.1:8787:8787` keeps it off the LAN while still being reachable from the host (and from a
  proxy on the host). If the proxy is another container, put both on one podman network and use
  `http://dietly-mcp:8787/mcp` — publishing is then unnecessary.
- On a non-loopback bind the SDK's Host/Origin (DNS-rebinding) pinning is **disabled**, because the
  public host name is unknown to the container. That is why a token is required there: use TLS, keep
  the token secret, and if you want header pinning as well, terminate TLS and forward to the
  container over a private network.
- Don't put the port on the public internet without TLS. The bearer token travels in a header.

## 9. Connecting clients (Open WebUI, others)

**Open WebUI** (v0.6.31+ has native MCP and speaks Streamable HTTP only): add a native MCP server with

- URL: `http://<host>:8787/mcp` (or `https://dietly.example.com/mcp`)
- Header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

Exact field names differ between versions (UI: *Settings → Tools/Integrations → MCP*); what matters is
the URL ending in `/mcp`, the bearer header, and that the tool names arrive as `dietly_*`. HTTP
(not stdio) is required for a containerised Open WebUI.

**Anything else that speaks Streamable HTTP** — point it at the same URL/header. Quick check without a
client, scripted end to end (from the host, using the project venv):

```fish
set -gx T (openssl rand -hex 32)
podman run -d --rm --name dietly-probe -p 127.0.0.1:8787:8787 -e MCP_AUTH_TOKEN=$T ghcr.io/psampir/dietly-mcp:0.2.0
curl -s http://127.0.0.1:8787/healthz                                        # 200, no auth
curl -si -X POST http://127.0.0.1:8787/mcp -H 'content-type: application/json' \
     -H 'accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -1           # 401
# authenticated tools/list (session handshake):
curl -s -X POST http://127.0.0.1:8787/mcp \
     -H "authorization: Bearer $T" -H 'content-type: application/json' \
     -H 'accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
     -D - -o /dev/null | grep -i '^mcp-session-id'
podman stop dietly-probe
```

**stdio** clients (OpenCode and friends) keep working unchanged. They never need a token: stdio has
no network surface. Point them at the `dietly-mcp-server` console script (or
`uv run --directory /path/to/dietly-mcp dietly-mcp-server` when it is not installed globally).

### OpenCode (V2)

Add the server to your OpenCode config (`opencode.jsonc` for one project, or `opencode.json`
globally):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "dietly": {
        "type": "local",
        "command": ["dietly-mcp-server"],
        "codemode": false
      }
    }
  }
}
```

- OpenCode names each tool `<server>_<tool>`, so the tools appear as `dietly_status`, `dietly_orders`,
  `dietly_deliveries`, `dietly_day`, `dietly_options`, `dietly_switch`, `dietly_apply` and
  `dietly_revert`.
- `codemode` defaults to `true`; with it, tools are reached through the Code Mode `tools` object
  (`tools.dietly.dietly_status(...)`) rather than as native tools. Set `codemode: false` to keep them
  **native** so permissions/allow-lists can target `dietly_*` directly.
- Optional `timeout` overrides exist (defaults: startup `30s`, catalog `30s`, execution `12h`).

Or add it from the CLI:

```fish
opencode mcp add dietly -- dietly-mcp-server    # add --global for every project
opencode mcp list                                # verify the server starts and its tools are discovered
```

### Running the server directly (no container)

The same binary serves both transports, so you can run it from a checkout or a venv:

```fish
dietly-mcp-server --transport streamable-http            # 127.0.0.1:8787 by default
dietly-mcp-server --transport streamable-http --host 0.0.0.0 --port 8787
```

| Option | Env | Default | Notes |
|---|---|---|---|
| `--transport` | `DIETLY_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `--host` | `DIETLY_HTTP_HOST` | `127.0.0.1` | a **non-loopback** bind requires `MCP_AUTH_TOKEN` |
| `--port` | `DIETLY_HTTP_PORT` | `8787` | |
| `--version` | — | | print the version and exit |

In HTTP mode logs go to **stdout** (containers, `podman logs`); stdio mode is untouched.

## 10. Updating

```fish
# latest release
podman pull ghcr.io/psampir/dietly-mcp:latest
systemctl --user restart dietly-mcp         # quadlet
# or, without systemd:
podman rm -f dietly-mcp && podman run -d ... ghcr.io/psampir/dietly-mcp:latest
```

Pin a version (`:0.2.0`) for reproducible deployments and bump deliberately: a bugfix release cannot
then change the tool contract under you. The `/data` volume carries the login session and the audit
log across updates, so an update does not log in again and `dietly_revert` can still see past writes.

With quadlet you can automate it: add `AutoUpdate=registry` to the `[Container]` section and run
`systemctl --user enable --now podman-auto-update.timer`.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Container exits immediately with `Refusing to bind 0.0.0.0 without MCP_AUTH_TOKEN` | Set `MCP_AUTH_TOKEN`, or bind `127.0.0.1`. This is the intended interlock |
| `401 {"ok": false, "error": "Unauthorized"}` | Missing/incorrect `Authorization: Bearer <token>` header on the client |
| `421 Invalid Host header` | Only on a loopback bind: the SDK pins `Host`/`Origin` to localhost. Use `127.0.0.1`/`localhost` as the URL (or bind a non-loopback address with a token) |
| Client reports "connection closed" / empty reply on `/mcp` | A proxy is buffering SSE or timing out too early — see §8 |
| Tools return `{"ok": false, ...}` about credentials/session | `/healthz` shows `credentialsConfigured: false` — set `DIETLY_EMAIL`/`DIETLY_PASSWORD` (or mount `/config/credentials.env`). Details are in the container logs (`podman logs dietly-mcp`) |
| `health: starting` then `unhealthy` | `/healthz` is not answering: check the port mapping, and that `DIETLY_HTTP_PORT` still matches the published port |
| Order changed unexpectedly | Check `/data/audit.jsonl` (`{"event": "switch", ...}` records with `from`/`to`) and use `dietly_revert`, or `podman exec` into the container and run `dietly-mcp audit` |

## 12. Publishing a release (maintainer)

The workflows are ready; the **tag** is the release trigger and you create it:

```fish
# 1. the version in pyproject.toml must equal the tag (the workflow enforces this)
#    (bump it in the usual commit that prepares the release)
grep '^version' pyproject.toml

# 2. tag and push — this runs lint + tests, builds linux/amd64+linux/arm64,
#    pushes to GHCR with provenance + SBOM, and creates the GitHub Release
git tag v0.2.0
git push origin v0.2.0

# 3. watch it
gh run watch
gh release view v0.2.0
podman pull ghcr.io/psampir/dietly-mcp:0.2.0
```

Yielding tags `0.2.0`, `0.2` and `latest` on `ghcr.io/psampir/dietly-mcp` for `v0.2.0`
(pre-release tags such as `v0.2.0-rc1` skip `latest`). The image is built by the workflow, not by
`git push` to `main`; a plain push only runs CI (lint + tests).

After the **first** successful push, make the GHCR package public once (GitHub → your profile →
*Packages* → `dietly-mcp` → *Package settings* → *Change visibility* → Public), otherwise every
consumer needs `podman login ghcr.io`. If you would rather keep it private, publish the token used for
pulls *read-only* (`read:packages`).

To re-tag a release (e.g. re-publish `latest` after a bad build), delete the release and tag on GitHub,
fix, and push the tag again — never move a tag that has already been pulled.

arm64 + provenance verification is a follow-up on real hardware: the local build/test loop in this
repository is amd64-only (`podman build --platform linux/amd64`), while CI cross-builds arm64 via QEMU
— verify `podman run --platform linux/arm64 ghcr.io/psampir/dietly-mcp:<version>` on an ARM host (or an
Apple-silicon/Raspberry-Pi box) before relying on it.
