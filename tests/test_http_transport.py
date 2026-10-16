"""Tests for the Streamable-HTTP transport (:mod:`dietly_mcp.http_server`).

Everything is in-process: the app is driven with Starlette's ``TestClient`` (ASGI,
no socket, no network) and the transport policy is exercised as pure functions.
The tests cover the F4 contract:

* the bearer-auth matrix (missing / wrong / valid token) and that a rejected
  request never reaches a tool;
* loopback-without-token allowed, non-loopback-without-token **refused**;
* ``GET /healthz`` shape — and that it leaks nothing, not even with credentials
  and a session present;
* booting without credentials and serving ``/healthz`` anyway;
* transport/host/port flag + env parsing (CLI flags win over env);
* write serialisation: two writes can never overlap.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from dietly_mcp import http_server, mcp_server
from dietly_mcp.errors import MissingCredentialsError

TOKEN = "test-token-not-a-secret"  # noqa: S105 - a dummy used only in this file
EMAIL = "dummy-user@example.com"

#: A loopback base URL: the app pins Host to localhost, and TestClient's default
#: ``testserver`` host would (correctly) be refused with 421 by DNS-rebinding
#: protection — the same protection that keeps a browser from reaching 127.0.0.1.
BASE_URL = "http://127.0.0.1:8787"

SESSION_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def dietly_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config/state roots at a throwaway tree.

    ``DIETLY_CONFIG_DIR`` / ``DIETLY_STATE_DIR`` are exactly what a container
    sets (``/config`` / ``/data``), so this both isolates the tests and pins the
    parent-directory override behaviour.
    """
    root = tmp_path / "dietly"
    monkeypatch.setenv("DIETLY_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("DIETLY_STATE_DIR", str(root / "state"))
    return root


@pytest.fixture(autouse=True)
def _no_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the module-level client cache (or a real connect) leak between tests."""
    monkeypatch.setattr(mcp_server, "_CLIENT", None)


@pytest.fixture
def app() -> Any:
    """The real ASGI app (MCP ``/mcp`` + ``/healthz``), built with a token."""

    def _build(*, token: str | None = TOKEN, host: str = http_server.DEFAULT_HOST) -> Any:
        return http_server.build_app(
            mcp_server.server, transport=http_server.HTTP_TRANSPORT, host=host, token=token
        )

    return _build


def _sse_json(text: str) -> dict[str, Any]:
    """Return the JSON-RPC payload of an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE payload in: {text[:200]!r}")


def _rpc(client: TestClient, payload: dict[str, Any], **kwargs: Any) -> Any:
    """POST a JSON-RPC message to ``/mcp`` with the MCP headers, plus any extras."""
    headers = {**SESSION_HEADERS, **kwargs.pop("headers", {})}
    return client.post("/mcp", json=payload, headers=headers, **kwargs)


def _initialize(client: TestClient, **kwargs: Any) -> str:
    """Run the MCP handshake and return the session id."""
    response = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        **kwargs,
    )
    assert response.status_code == 200, response.text
    return response.headers["mcp-session-id"]


# --------------------------------------------------------------------------- #
# Loopback / bind policy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.53", "::1", "[::1]", "localhost", "LOCALHOST", " 127.0.0.1 "],
)
def test_loopback_hosts_are_recognised(host: str) -> None:
    assert http_server.is_loopback(host) is True


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "dietly.example.com", ""]
)
def test_non_loopback_hosts_are_recognised(host: str) -> None:
    assert http_server.is_loopback(host) is False


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_without_a_token_is_allowed(host: str) -> None:
    assert http_server.unsafe_bind_reason(host, None) is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5"])
def test_non_loopback_bind_without_a_token_is_refused(host: str) -> None:
    reason = http_server.unsafe_bind_reason(host, None)

    assert reason is not None
    assert host in reason
    assert http_server.AUTH_ENV in reason  # the message names the fix
    assert "127.0.0.1" in reason


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.5", "127.0.0.1"])
def test_a_token_allows_any_bind(host: str) -> None:
    assert http_server.unsafe_bind_reason(host, TOKEN) is None


# --------------------------------------------------------------------------- #
# Transport / host / port resolution
# --------------------------------------------------------------------------- #
def test_transport_defaults_to_stdio() -> None:
    assert http_server.resolve_transport(None, None) == http_server.STDIO_TRANSPORT


def test_transport_comes_from_the_env_then_the_flag() -> None:
    assert http_server.resolve_transport(None, "streamable-http") == "streamable-http"
    assert http_server.resolve_transport(None, " STREAMABLE-HTTP ") == "streamable-http"
    # The flag wins over the environment.
    assert http_server.resolve_transport("stdio", "streamable-http") == "stdio"


def test_an_unknown_transport_is_refused_with_the_valid_ones() -> None:
    with pytest.raises(SystemExit, match="Unknown transport"):
        http_server.resolve_transport("carrier-pigeon", None)

    with pytest.raises(SystemExit, match="streamable-http"):
        http_server.resolve_transport(None, "http")


def test_host_and_port_defaults_and_overrides() -> None:
    assert http_server.resolve_host(None, None) == http_server.DEFAULT_HOST
    assert http_server.resolve_host(None, "0.0.0.0") == "0.0.0.0"
    assert http_server.resolve_host("10.0.0.5", "0.0.0.0") == "10.0.0.5"

    assert http_server.resolve_port(None, None) == http_server.DEFAULT_PORT
    assert http_server.resolve_port(None, "9000") == 9000
    assert http_server.resolve_port(9001, "9000") == 9001


@pytest.mark.parametrize("bad", ["http", "0", "65536", "-1"])
def test_an_unusable_port_is_refused(bad: str) -> None:
    with pytest.raises(SystemExit, match="port"):
        http_server.resolve_port(None, bad)


def test_an_empty_port_env_var_means_unset() -> None:
    """An unset/empty container variable must fall back to the default, not crash."""
    assert http_server.resolve_port(None, "") == http_server.DEFAULT_PORT
    assert http_server.resolve_host(None, "") == http_server.DEFAULT_HOST


def test_cli_parses_transport_host_port() -> None:
    args = mcp_server.build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"]
    )

    assert (args.transport, args.host, args.port) == ("streamable-http", "0.0.0.0", 9000)
    # Every option is optional: env vars alone can configure the container.
    defaults = mcp_server.build_parser().parse_args([])
    assert (defaults.transport, defaults.host, defaults.port) == (None, None, None)


def test_an_unknown_transport_flag_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit):
        mcp_server.build_parser().parse_args(["--transport", "nope"])


# --------------------------------------------------------------------------- #
# main(): stdio stays the default, HTTP wires host/port/token
# --------------------------------------------------------------------------- #
def test_main_runs_stdio_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(mcp_server.server, "run", lambda transport: seen.append(transport))
    monkeypatch.setattr(http_server, "serve", lambda *a, **k: pytest.fail("must not serve HTTP"))

    mcp_server.main([])

    assert seen == ["stdio"]


def test_main_serves_http_from_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(http_server, "serve", lambda server, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)

    mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"])

    assert len(calls) == 1
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["port"] == 9000
    assert calls[0]["token"] == TOKEN
    assert calls[0]["transport"] == "streamable-http"


def test_main_serves_http_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(http_server, "serve", lambda server, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("DIETLY_TRANSPORT", "streamable-http")
    monkeypatch.setenv("DIETLY_HTTP_HOST", "::")
    monkeypatch.setenv("DIETLY_HTTP_PORT", "9100")
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)

    mcp_server.main([])

    assert calls[0]["host"] == "::"
    assert calls[0]["port"] == 9100


def test_main_refuses_a_non_loopback_bind_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole CLI path refuses before a socket is opened."""
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0"])

    message = str(excinfo.value)
    assert "MCP_AUTH_TOKEN" in message
    assert "0.0.0.0" in message


def test_a_blank_token_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whitespace-only token must not be mistaken for real auth."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(http_server, "serve", lambda server, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("MCP_AUTH_TOKEN", "   ")

    mcp_server.main(["--transport", "streamable-http"])

    assert calls[0]["token"] is None


def test_serve_refuses_an_unsafe_bind_before_opening_a_socket() -> None:
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        http_server.serve(mcp_server.server, host="0.0.0.0", token=None)


# --------------------------------------------------------------------------- #
# Bearer auth: the middleware on its own (raw ASGI, no HTTP client)
# --------------------------------------------------------------------------- #
class _Recorder:
    """A minimal ASGI app that records being reached."""

    def __init__(self) -> None:
        self.scopes: list[Any] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})


def _call(middleware: Any, scope: dict[str, Any]) -> tuple[int, bytes, bool]:
    """Drive a pure-ASGI middleware with one scope; return ``(status, body, reached)``."""
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    import asyncio

    asyncio.run(middleware(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, start["status"] == 200


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], 401),
        ([(b"authorization", b"")], 401),
        ([(b"authorization", b"Bearer")], 401),
        ([(b"authorization", b"Bearer ")], 401),
        ([(b"authorization", b"Bearer wrong")], 401),
        ([(b"authorization", b"Bearer test-token-not-a-secre")], 401),  # truncated
        ([(b"authorization", b"Basic dGVzdA==")], 401),
        ([(b"authorization", b"Bearer test-token-not-a-secret")], 200),
        ([(b"authorization", b"bearer test-token-not-a-secret")], 200),  # scheme is caseless
        ([(b"X-Api-Key", b"test-token-not-a-secret")], 401),  # not the bearer header
    ],
)
def test_bearer_middleware_matrix(headers: list[tuple[bytes, bytes]], expected: int) -> None:
    inner = _Recorder()
    middleware = http_server.BearerAuthMiddleware(inner, token=TOKEN)
    scope = {"type": "http", "path": "/mcp", "headers": headers}

    status, body, reached = _call(middleware, scope)

    assert status == expected
    if expected == 401:
        assert reached is False, "a rejected request must never reach the app"
        assert b"Unauthorized" in body
        assert inner.scopes == []
    else:
        assert len(inner.scopes) == 1


def test_bearer_middleware_forwards_lifespan_and_exempts_healthz() -> None:
    """The MCP session manager is started from the lifespan scope, so it must pass."""
    inner = _Recorder()
    middleware = http_server.BearerAuthMiddleware(inner, token=TOKEN)

    status, _, _ = _call(middleware, {"type": "lifespan", "path": "/mcp", "headers": []})
    assert status == 200
    status, _, _ = _call(middleware, {"type": "http", "path": "/healthz", "headers": []})
    assert status == 200
    status, _, _ = _call(middleware, {"type": "http", "path": "/healthz/", "headers": []})
    assert status == 200
    assert len(inner.scopes) == 3


def test_bearer_middleware_rejects_an_empty_token() -> None:
    with pytest.raises(ValueError, match="non-empty token"):
        http_server.BearerAuthMiddleware(_Recorder(), token="")


# --------------------------------------------------------------------------- #
# Bearer auth: end-to-end over the ASGI app
# --------------------------------------------------------------------------- #
def test_mcp_endpoint_requires_a_token(app: Any) -> None:
    with TestClient(app(), base_url=BASE_URL) as client:
        missing = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        wrong = _rpc(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**SESSION_HEADERS, "authorization": "Bearer nope"},
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert response.json()["ok"] is False
        # No session was ever created, so nothing was executed.
        assert "mcp-session-id" not in response.headers


def test_a_rejected_request_never_touches_the_account(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 must be answered before the client is even built."""
    built: list[int] = []

    def _connect() -> Any:
        built.append(1)
        raise AssertionError("the client must not be constructed for an unauthenticated call")

    monkeypatch.setattr(mcp_server, "_connect", _connect)

    with TestClient(app(), base_url=BASE_URL) as client:
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "dietly_status"},
            },
        )

    assert response.status_code == 401
    assert built == []


def test_authenticated_tools_list_returns_the_eight_tools(app: Any) -> None:
    auth = {"authorization": f"Bearer {TOKEN}"}
    with TestClient(app(), base_url=BASE_URL) as client:
        session_id = _initialize(client, headers=auth)
        response = _rpc(
            client,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**auth, "mcp-session-id": session_id},
        )

    assert response.status_code == 200
    names = sorted(tool["name"] for tool in _sse_json(response.text)["result"]["tools"])
    assert names == [
        "dietly_apply",
        "dietly_day",
        "dietly_deliveries",
        "dietly_options",
        "dietly_orders",
        "dietly_revert",
        "dietly_status",
        "dietly_switch",
    ]


def test_http_tool_call_without_credentials_is_an_actionable_error(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booting without credentials is fine; the *tool* reports the problem."""

    def _connect() -> Any:
        raise MissingCredentialsError("No credentials file found.")

    monkeypatch.setattr(mcp_server, "_connect", _connect)

    auth = {"authorization": f"Bearer {TOKEN}"}
    with TestClient(app(), base_url=BASE_URL) as client:
        session_id = _initialize(client, headers=auth)
        response = _rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "dietly_status", "arguments": {}},
            },
            headers={**auth, "mcp-session-id": session_id},
        )

    payload = _sse_json(response.text)
    result = json.loads(payload["result"]["content"][0]["text"])
    assert result["ok"] is False
    assert "credential" in result["error"].lower()
    assert "DIETLY_EMAIL" in result["hint"]


def test_loopback_server_without_a_token_accepts_requests(app: Any) -> None:
    with TestClient(app(token=None), base_url=BASE_URL) as client:
        health = client.get("/healthz")
        session_id = _initialize(client)

    assert health.status_code == 200
    assert health.json()["authRequired"] is False
    assert session_id


# --------------------------------------------------------------------------- #
# /healthz
# --------------------------------------------------------------------------- #
def test_healthz_shape_and_no_auth_required(app: Any) -> None:
    with TestClient(app(), base_url=BASE_URL) as client:
        response = client.get("/healthz")  # deliberately no Authorization header

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "dietly-mcp"
    assert payload["version"]
    assert payload["transport"] == "streamable-http"
    assert payload["mcpPath"] == "/mcp"
    assert payload["authRequired"] is True
    assert payload["credentialsConfigured"] is False
    assert payload["sessionConfigured"] is False
    assert isinstance(payload["uptimeSeconds"], int | float)
    assert payload["uptimeSeconds"] >= 0
    assert set(payload) == {
        "status",
        "service",
        "version",
        "transport",
        "mcpPath",
        "uptimeSeconds",
        "authRequired",
        "credentialsConfigured",
        "sessionConfigured",
    }


def test_healthz_serves_without_credentials_and_without_a_session(app: Any) -> None:
    """Container start-up: the app must come up before any credentials exist."""
    with TestClient(app(), base_url=BASE_URL) as client:
        payload = client.get("/healthz").json()

    assert payload["credentialsConfigured"] is False
    assert payload["sessionConfigured"] is False


def test_healthz_reports_readiness_but_never_the_secrets(
    app: Any, dietly_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentials + session present → booleans ``true``, and **nothing** leaked."""
    config_dir = dietly_dirs / "config"
    state_dir = dietly_dirs / "state"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (config_dir / "credentials.env").write_text(
        f"DIETLY_EMAIL={EMAIL}\nDIETLY_PASSWORD=hunter2-do-not-leak\n", encoding="utf-8"
    )
    (state_dir / "session.json").write_text('{"SESSION": "secret-cookie"}', encoding="utf-8")
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)

    with TestClient(app(), base_url=BASE_URL) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["credentialsConfigured"] is True
    assert response.json()["sessionConfigured"] is True
    body = response.text
    for secret in (
        EMAIL,
        "hunter2-do-not-leak",
        "secret-cookie",
        TOKEN,
        str(config_dir),
        str(state_dir),
    ):
        assert secret not in body


def test_healthz_reports_the_amount_of_uptime(app: Any) -> None:
    runtime = http_server.runtime()
    runtime.now = lambda: runtime.started_at + 42.5  # type: ignore[method-assign]

    with TestClient(app(), base_url=BASE_URL) as client:
        payload = client.get("/healthz").json()

    assert payload["uptimeSeconds"] == 42.5
    assert payload["transport"] == "streamable-http"


def test_healthz_reports_the_configured_transport(app: Any) -> None:
    """``/healthz`` reflects the transport the app was built for, not a constant."""
    built = http_server.build_app(
        mcp_server.server, transport=http_server.STDIO_TRANSPORT, host=http_server.DEFAULT_HOST
    )

    with TestClient(built, base_url=BASE_URL) as client:
        assert client.get("/healthz").json()["transport"] == http_server.STDIO_TRANSPORT

    with TestClient(app(), base_url=BASE_URL) as client:
        assert client.get("/healthz").json()["transport"] == http_server.HTTP_TRANSPORT


# --------------------------------------------------------------------------- #
# Write serialisation
# --------------------------------------------------------------------------- #
def test_concurrent_writes_never_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two switches must never race: the write lock admits one at a time."""
    observed = {"active": 0, "max": 0, "calls": 0}
    guard = threading.Lock()

    def slow_run(build: Any) -> dict[str, Any]:
        with guard:
            observed["active"] += 1
            observed["max"] = max(observed["max"], observed["active"])
        time.sleep(0.05)  # a real (slow) HTTP round-trip would sit right here
        with guard:
            observed["active"] -= 1
            observed["calls"] += 1
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_run", slow_run)

    def switch(i: int) -> dict[str, Any]:
        return mcp_server.dietly_switch("2026-10-29", "Obiad", 2400 + i, dry_run=False)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in [pool.submit(switch, i) for i in range(4)]]

    assert observed["calls"] == 4
    assert observed["max"] == 1, "two write operations were in flight at once"
    assert all(result["ok"] for result in results)


def test_applying_a_batch_and_a_switch_share_one_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """``dietly_apply`` and ``dietly_switch`` serialise against each other too."""
    observed = {"active": 0, "max": 0, "calls": 0}
    guard = threading.Lock()

    def slow_run(build: Any) -> dict[str, Any]:
        with guard:
            observed["active"] += 1
            observed["max"] = max(observed["max"], observed["active"])
        time.sleep(0.05)
        with guard:
            observed["active"] -= 1
            observed["calls"] += 1
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_run", slow_run)
    entries = [{"date": "2026-10-29", "slot": "Obiad", "to_id": 2401}]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(mcp_server.dietly_switch, "2026-10-29", "Obiad", 2401, None, False),
            pool.submit(mcp_server.dietly_apply, entries, None, False),
        ]
        [future.result() for future in futures]

    assert observed["calls"] == 2
    assert observed["max"] == 1


def test_reads_are_not_blocked_by_the_write_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read tool must not wait for the write lock (only writes serialise)."""
    assert mcp_server._WRITE_LOCK.acquire(blocking=False)
    try:
        monkeypatch.setattr(mcp_server, "_run", lambda build: {"ok": True})
        assert mcp_server.dietly_status() == {"ok": True}
    finally:
        mcp_server._WRITE_LOCK.release()


# --------------------------------------------------------------------------- #
# Runtime / app assembly
# --------------------------------------------------------------------------- #
def test_health_route_is_registered_only_once(app: Any) -> None:
    http_server.install_health_route(mcp_server.server)
    http_server.install_health_route(mcp_server.server)

    paths = [route.path for route in app().routes]
    assert paths.count(http_server.HEALTH_PATH) == 1
    assert paths.count(http_server.MCP_PATH) == 1


def test_build_app_returns_a_starlette_app_with_both_routes(app: Any) -> None:
    built = app()

    assert isinstance(built, Starlette)
    paths = {route.path for route in built.routes}
    assert http_server.MCP_PATH in paths
    assert http_server.HEALTH_PATH in paths


def test_non_loopback_bind_disables_host_pinning(app: Any) -> None:
    """Behind a proxy the Host header is the proxy's, so only the token guards us."""
    settings = http_server._transport_security("0.0.0.0")

    assert settings.enable_dns_rebinding_protection is False
    assert http_server._transport_security("127.0.0.1").enable_dns_rebinding_protection is True


def test_proxy_style_healthz_route_helper_never_raises_without_dirs() -> None:
    """``/healthz`` must not raise when the config/state dirs do not exist yet."""
    assert http_server.health_payload()["credentialsConfigured"] is False


def test_configure_logging_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    http_server.configure_logging(logging.INFO)
    logging.getLogger("dietly_mcp.test").info("hello-stdout")
    logging.shutdown()

    captured = capsys.readouterr()
    assert "hello-stdout" in captured.out
    assert "hello-stdout" not in captured.err


def test_default_config_and_state_dirs_are_env_overridable(
    dietly_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dietly_mcp import config

    assert config.config_dir() == dietly_dirs / "config"
    assert config.state_dir() == dietly_dirs / "state"
    assert config.default_credentials_path() == dietly_dirs / "config" / "credentials.env"
    assert config.default_session_path() == dietly_dirs / "state" / "session.json"
    assert config.default_audit_path() == dietly_dirs / "state" / "audit.jsonl"

    monkeypatch.setenv("DIETLY_CONFIG_DIR", "/config")
    monkeypatch.setenv("DIETLY_STATE_DIR", "/data")
    assert config.default_credentials_path() == Path("/config/credentials.env")
    assert config.default_session_path() == Path("/data/session.json")
    assert config.default_audit_path() == Path("/data/audit.jsonl")


def test_healthz_helper_reports_a_configured_session(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from dietly_mcp import config

    assert config.credentials_configured() is False
    assert config.session_configured() is False

    monkeypatch.setenv("DIETLY_EMAIL", EMAIL)
    monkeypatch.setenv("DIETLY_PASSWORD", "dummy-password")
    session_path = config.default_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("{}", encoding="utf-8")

    assert config.credentials_configured() is True
    assert config.session_configured() is True
