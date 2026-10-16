"""Streamable-HTTP transport for the Dietly MCP server (alongside stdio).

``dietly-mcp-server`` speaks **stdio** by default (what OpenCode and other local
clients spawn) and **Streamable HTTP** when asked to — the transport Open WebUI
(native MCP, v0.6.31+) and other remote clients need. This module owns the HTTP
half, keeping :mod:`dietly_mcp.mcp_server` (the tools) transport-agnostic:

* :func:`build_app` turns an ``MCPServer`` into an ASGI ``Starlette`` app with the
  MCP endpoint (``/mcp``), a public ``GET /healthz`` and — when a token is set —
  a bearer-auth gate;
* :func:`serve` runs that app under uvicorn on the requested host/port;
* :func:`unsafe_bind_reason` is the safety interlock: **this endpoint can modify
  the user's real Dietly order**, so a non-loopback bind without
  ``MCP_AUTH_TOKEN`` is refused instead of merely warned about.

Safety rules, in order:

1. ``MCP_AUTH_TOKEN`` set → every request except ``GET /healthz`` must carry
   ``Authorization: Bearer <token>``; anything else is answered ``401`` without
   running a single tool.
2. ``MCP_AUTH_TOKEN`` unset → only loopback binds (``127.0.0.1``, ``::1``, any
   ``127/8``/``::1`` address, ``localhost``) are allowed. A wider bind aborts
   startup with a message that says exactly what to do.
3. Credentials are never required to start: the process boots without them, and
   ``/healthz`` reports readiness with booleans only. Tool calls then return the
   usual actionable ``{"ok": false, ...}`` errors.

Nothing here ever logs a secret: the token is only ever compared, never printed,
and the bind banner reports *whether* auth is required, not what it is.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from . import __version__
from .config import (
    config_dir,
    credentials_configured,
    load_settings,
    session_configured,
    state_dir,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.mcpserver import MCPServer
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "AUTH_ENV",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "HTTP_TRANSPORT",
    "MCP_PATH",
    "STDIO_TRANSPORT",
    "TRANSPORTS",
    "BearerAuthMiddleware",
    "Runtime",
    "build_app",
    "configure_logging",
    "is_loopback",
    "resolve_host",
    "resolve_port",
    "resolve_transport",
    "runtime",
    "serve",
    "unsafe_bind_reason",
]

#: The transport that keeps working exactly as before (OpenCode spawns it).
STDIO_TRANSPORT = "stdio"
#: The network transport (Open WebUI and other remote MCP clients).
HTTP_TRANSPORT = "streamable-http"
TRANSPORTS = (STDIO_TRANSPORT, HTTP_TRANSPORT)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: Where the MCP endpoint lives, and the one public (unauthenticated) path.
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
#: Paths that never require the bearer token (health checks only).
EXEMPT_PATHS = (HEALTH_PATH,)

#: Environment variable holding the bearer token. Set it to expose the server
#: anywhere but loopback; leave it unset to keep the server local-only.
AUTH_ENV = "MCP_AUTH_TOKEN"
#: Environment variables selecting the transport, the bind address and the port.
TRANSPORT_ENV = "DIETLY_TRANSPORT"
HOST_ENV = "DIETLY_HTTP_HOST"
PORT_ENV = "DIETLY_HTTP_PORT"

_SERVICE_NAME = "dietly-mcp"


# --------------------------------------------------------------------------- #
# Bind policy
# --------------------------------------------------------------------------- #
def _strip_brackets(host: str) -> str:
    """Return ``host`` without the IPv6 literal brackets URL syntax uses."""
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


def is_loopback(host: str) -> bool:
    """Return whether ``host`` names this machine only.

    ``localhost`` counts, and so does any loopback address of either family
    (``127.0.0.1``, ``127.0.0.53``, ``::1``). Anything else — including
    ``0.0.0.0``, ``::`` and every LAN/host name — is a wider bind.
    """
    text = _strip_brackets(host)
    if not text:
        return False
    if text.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def unsafe_bind_reason(host: str, token: str | None) -> str | None:
    """Return why binding ``host`` is refused, or ``None`` when it is allowed.

    A non-loopback bind requires ``MCP_AUTH_TOKEN``: without it, anyone who can
    reach the port could read the account and rewrite the real order.
    """
    if token or is_loopback(host):
        return None
    return (
        f"Refusing to bind {host} without {AUTH_ENV}.\n"
        "This endpoint can read the account and MODIFY the real Dietly order, so it is\n"
        "loopback-only unless a bearer token is set. Either:\n"
        f"  * export {AUTH_ENV}=$(openssl rand -hex 32)   # clients then send\n"
        "                                        Authorization: Bearer <token>\n"
        f"  * or bind {DEFAULT_HOST} (the default) and reach it over an SSH tunnel."
    )


# --------------------------------------------------------------------------- #
# CLI/env resolution
# --------------------------------------------------------------------------- #
def resolve_transport(flag: str | None, env: str | None) -> str:
    """Pick the transport from ``--transport``, then ``DIETLY_TRANSPORT``, then stdio.

    Raises:
        SystemExit: the value is not one of :data:`TRANSPORTS`.
    """
    value = (flag or env or STDIO_TRANSPORT).strip().lower()
    if value not in TRANSPORTS:
        allowed = ", ".join(TRANSPORTS)
        source = "--transport" if flag else TRANSPORT_ENV
        raise SystemExit(
            f"Unknown transport {value!r} (from {source}). "
            f"Valid transports: {allowed} (default {STDIO_TRANSPORT})."
        )
    return value


def resolve_host(flag: str | None, env: str | None) -> str:
    """Pick the bind host from ``--host``, then ``DIETLY_HTTP_HOST``, then loopback."""
    return (flag or env or DEFAULT_HOST).strip()


def resolve_port(flag: int | None, env: str | None) -> int:
    """Pick the port from ``--port``, then ``DIETLY_HTTP_PORT``, then ``8787``.

    Raises:
        SystemExit: the value is not a usable TCP port.
    """
    if flag is not None:
        port, source = flag, "--port"
    elif env:
        port, source = env.strip(), PORT_ENV
    else:
        return DEFAULT_PORT
    try:
        number = int(port)
    except (TypeError, ValueError):
        raise SystemExit(
            f"Invalid port {port!r} (from {source}): expected an integer."
        ) from None
    if not 1 <= number <= 65535:
        raise SystemExit(f"Invalid port {number} (from {source}): expected 1-65535.")
    return number


# --------------------------------------------------------------------------- #
# Runtime facts reported by GET /healthz
# --------------------------------------------------------------------------- #
@dataclass
class Runtime:
    """What the live app knows about itself; reported by ``/healthz``.

    Mutated in place by :func:`build_app` so the health route (registered once on
    the shared ``MCPServer``) always reports the current process's facts. Holds
    booleans and labels only — never an email, token or order datum.
    """

    transport: str = STDIO_TRANSPORT
    #: Monotonic instant the app was built; ``/healthz`` reports the delta.
    started_at: float = field(default_factory=time.monotonic)
    auth_required: bool = False
    #: Overridable so tests can assert an exact uptime.
    now: Any = time.monotonic

    def uptime(self) -> float:
        """Seconds since :func:`build_app` created the app (never negative)."""
        return max(0.0, round(self.now() - self.started_at, 3))


_RUNTIME = Runtime()


def runtime() -> Runtime:
    """Return the process-wide :class:`Runtime` that ``/healthz`` reads."""
    return _RUNTIME


def health_payload() -> dict[str, Any]:
    """Build the ``/healthz`` body: liveness plus (boolean) readiness.

    Deliberately free of account data: no email, no token, no order, no path.
    ``credentialsConfigured`` / ``sessionConfigured`` are what an operator (or a
    container healthcheck) needs to see, and they are booleans.
    """
    settings = load_settings()
    return {
        "status": "ok",
        "service": _SERVICE_NAME,
        "version": __version__,
        "transport": _RUNTIME.transport,
        "mcpPath": MCP_PATH,
        "uptimeSeconds": _RUNTIME.uptime(),
        "authRequired": _RUNTIME.auth_required,
        "credentialsConfigured": credentials_configured(settings.credentials_path),
        "sessionConfigured": session_configured(settings.session_path),
    }


async def _healthz(_request: Any) -> JSONResponse:
    """``GET /healthz`` — public, credential-free, 200 as long as we are alive."""
    return JSONResponse(health_payload())


# --------------------------------------------------------------------------- #
# Bearer-token gate
# --------------------------------------------------------------------------- #
def _bearer_token(headers: list[tuple[bytes, bytes]] | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header, else ``None``."""
    for name, value in headers or []:
        if name.lower() == b"authorization":
            scheme, _, rest = value.decode("latin-1").strip().partition(" ")
            if scheme.casefold() == "bearer" and rest.strip():
                return rest.strip()
    return None


class BearerAuthMiddleware:
    """Pure-ASGI gate requiring ``Authorization: Bearer <token>`` on MCP requests.

    Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it forwards the
    ``lifespan`` scope to the MCP app — the Streamable-HTTP session manager is
    started from that lifespan and the server would not accept sessions without
    it. A rejected request is answered ``401`` here, before any tool runs, so an
    unauthenticated caller can never reach Dietly.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        exempt_paths: tuple[str, ...] = EXEMPT_PATHS,
        realm: str = _SERVICE_NAME,
    ) -> None:
        if not token:
            raise ValueError("BearerAuthMiddleware needs a non-empty token")
        self.app = app
        self._token = token.encode()
        self._exempt = frozenset(exempt_paths)
        self._realm = realm

    def _exempt_path(self, path: str) -> bool:
        return path in self._exempt or path.rstrip("/") in self._exempt

    def _authorised(self, headers: list[tuple[bytes, bytes]] | None) -> bool:
        presented = _bearer_token(headers)
        if presented is None:
            return False
        return hmac.compare_digest(presented.encode(), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or self._exempt_path(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        if self._authorised(scope.get("headers")):
            await self.app(scope, receive, send)
            return
        await self._deny(send)

    async def _deny(self, send: Send) -> None:
        """Answer ``401`` in JSON, naming the header the client must send."""
        body = json.dumps(
            {
                "ok": False,
                "error": "Unauthorized",
                "hint": f"Send 'Authorization: Bearer <{AUTH_ENV}>'.",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", f'Bearer realm="{self._realm}"'.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- #
# App assembly
# --------------------------------------------------------------------------- #
_health_installed = False


def install_health_route(server: MCPServer) -> None:
    """Register ``GET /healthz`` on ``server`` exactly once.

    ``custom_route`` is the SDK's supported hook for endpoints outside the MCP
    protocol, and such routes are explicitly **not** behind MCP authorization —
    which is what a health probe needs (it must answer while credentials are
    missing, e.g. during container start-up).
    """
    global _health_installed
    if _health_installed:
        return
    server.custom_route(HEALTH_PATH, methods=["GET"], name="healthz")(_healthz)
    _health_installed = True


def _transport_security(host: str) -> Any:
    """Return the SDK's DNS-rebinding settings for this bind address.

    On loopback the strict default (Host/Origin pinned to localhost) is kept. A
    wider bind sits behind a reverse proxy with an unknown public host name, so
    header pinning is not usable there and is disabled — in that mode the bearer
    token is the access control (and :func:`unsafe_bind_reason` insists on one).
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if is_loopback(host):
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{DEFAULT_HOST}:*", "localhost:*", "[::1]:*"],
            allowed_origins=[
                f"http://{DEFAULT_HOST}:*",
                "http://localhost:*",
                "http://[::1]:*",
            ],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_app(
    server: MCPServer,
    *,
    transport: str = HTTP_TRANSPORT,
    host: str = DEFAULT_HOST,
    token: str | None = None,
) -> Starlette:
    """Build the ASGI app: ``/mcp`` (+ bearer gate when ``token`` is set) + ``/healthz``.

    The app is served by uvicorn (see :func:`serve`) rather than the SDK's own
    ``run(transport=...)`` helper, so that host/port are explicit, ``/healthz``
    is mounted, and an unsafe bind can be refused before a socket is opened.
    """
    runtime().transport = transport
    runtime().auth_required = bool(token)
    runtime().started_at = time.monotonic()
    install_health_route(server)

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=_transport_security(host),
        host=host,
    )
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return app


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: int | str = logging.INFO) -> None:
    """Send every log record (ours and uvicorn's) to **stdout**.

    Containers are read from stdout; ``force=True`` makes the call idempotent and
    overrides a previous configuration. Uvicorn is started with ``log_config=None``
    so its records propagate to this root handler instead of a stderr one.
    """
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format=_LOG_FORMAT,
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )


def _quoted_host(host: str) -> str:
    """Render ``host`` for a URL, bracketing IPv6 literals."""
    try:
        if ipaddress.ip_address(_strip_brackets(host)).version == 6:
            return f"[{_strip_brackets(host)}]"
    except ValueError:
        pass
    return host


def serve(
    server: MCPServer,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    transport: str = HTTP_TRANSPORT,
    log_level: int | str = logging.INFO,
) -> None:
    """Run the MCP server over Streamable HTTP until interrupted.

    Refuses to start on an unsafe bind (see :func:`unsafe_bind_reason`) and prints
    a banner saying where it listens and **whether** auth is required — never a
    secret. Credentials are not needed here: without them the server still starts
    and serves ``/healthz``; only tool calls report the missing session.

    Raises:
        SystemExit: the bind is unsafe. The message is the whole explanation.
    """
    reason = unsafe_bind_reason(host, token)
    if reason:
        raise SystemExit(reason)

    configure_logging(log_level)
    log = logging.getLogger("dietly_mcp.http")
    app = build_app(server, transport=transport, host=host, token=token)

    print(
        f"dietly-mcp {__version__} — {transport} on "
        f"http://{_quoted_host(host)}:{port}{MCP_PATH}",
        flush=True,
    )
    print(
        f"health:  http://{_quoted_host(host)}:{port}{HEALTH_PATH}  (no auth)\n"
        f"auth:    {'bearer token required' if token else 'loopback-only (no token set)'}\n"
        f"config:  {config_dir()}\n"
        f"state:   {state_dir()}\n"
        f"logging: stdout",
        flush=True,
    )
    if not is_loopback(host):
        log.warning(
            "binding %s disables Host/Origin (DNS-rebinding) pinning; "
            "keep %s set and terminate TLS in a reverse proxy in front of this port",
            host,
            AUTH_ENV,
        )

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,  # keep our stdout handler; uvicorn logs propagate to it
        access_log=True,
        server_header=False,
    )
