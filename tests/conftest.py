"""Shared pytest fixtures: temp settings and a mock HTTP transport.

No test in this suite touches the network — every request goes through an
:class:`httpx.MockTransport` whose handler raises on unexpected URLs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from dietly_mcp.config import Settings
from dietly_mcp.session import Session

#: The panel host serves both the login endpoint and the JSON API, and the
#: ``SESSION`` cookie it sets is host-only for that host.
LOGIN_URL = "https://panel.dietly.pl/api/auth/login"
BASE_URL = "https://panel.dietly.pl"

#: Value of the ``XSRF-TOKEN`` cookie handed out by the priming GET.
CSRF_TOKEN = "csrf-token-abc123"

#: What Dietly's Spring backend answers a state-changing call without a valid
#: CSRF token (observed live: HTTP 403, no Cloudflare markers).
CSRF_REJECTION_BODY = {
    "timestamp": "2026-10-16T14:38:21.386Z",
    "status": 403,
    "error": "Forbidden",
    "path": "/auth/login",
}


def csrf_cookie_header(token: str = CSRF_TOKEN) -> str:
    """Return a ``Set-Cookie`` header for the CSRF cookie (never HttpOnly)."""
    return f"XSRF-TOKEN={token}; Path=/"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient DIETLY_*/XDG_* variables from leaking into tests."""
    for name in (
        "DIETLY_EMAIL",
        "DIETLY_USERNAME",
        "DIETLY_PASSWORD",
        "DIETLY_CREDENTIALS_FILE",
        "DIETLY_SESSION_FILE",
        "DIETLY_BASE_URL",
        "DIETLY_LOGIN_URL",
        "DIETLY_COMPANY_ID",
        "DIETLY_USER_AGENT",
        "DIETLY_ACCEPT_LANGUAGE",
        "DIETLY_TIMEOUT",
        "DIETLY_CONNECT_TIMEOUT",
        "DIETLY_RETRIES",
        "DIETLY_IPV4_ONLY",
        "DIETLY_EDIT_CUTOFF_HOURS",
        "EMAIL",
        "USERNAME",
        "PASSWORD",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at throwaway config/state paths under ``tmp_path``.

    ``retries=0`` keeps the suite instant; retry behaviour has its own tests.
    """
    return Settings(
        base_url=BASE_URL,
        login_url=LOGIN_URL,
        company_id="wybormenu",
        user_agent="dietly-mcp-test/0.1",
        accept_language="pl-PL,pl;q=0.9",
        timeout=5.0,
        connect_timeout=2.0,
        retries=0,
        credentials_path=tmp_path / "config" / "credentials.env",
        session_path=tmp_path / "state" / "session.json",
    )


@pytest.fixture
def responses() -> dict[str, httpx.Response]:
    """Routing table for the mock transport; tests fill it in."""
    return {}


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def handler(responses: dict[str, httpx.Response], seen: list[httpx.Request]) -> Handler:
    """Return a handler that answers from ``responses`` and records requests."""

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.method} {request.url.path}"
        if key in responses:
            return responses[key]
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return _handler


@pytest.fixture
def seen() -> list[httpx.Request]:
    """Requests observed by the mock transport."""
    return []


@pytest.fixture
def transport(handler: Handler) -> httpx.MockTransport:
    """Mock transport wired to the ``handler`` fixture."""
    return httpx.MockTransport(handler)


@pytest.fixture
def session(settings: Settings, transport: httpx.MockTransport) -> Session:
    """A Session whose HTTP client uses the mock transport."""
    return Session(Session._new_client(settings, transport=transport), settings)


@pytest.fixture
def prime_csrf(responses: dict[str, httpx.Response]) -> Callable[[str], None]:
    """Register the priming GET that hands out the CSRF cookie.

    Calling the returned function with an alternative token simulates Spring
    rotating the token underneath us.
    """

    def _prime(token: str = CSRF_TOKEN) -> None:
        responses["GET /api/auth/login"] = httpx.Response(
            401,
            json={"status": 401, "error": "Unauthorized", "path": "/auth/login"},
            headers={"set-cookie": csrf_cookie_header(token)},
        )

    return _prime


@pytest.fixture
def login_ok(prime_csrf: Callable[[str], None], responses: dict[str, httpx.Response]) -> None:
    """Wire a complete successful login: primed CSRF cookie + 200 session cookie."""
    prime_csrf()
    responses["POST /api/auth/login"] = httpx.Response(
        200,
        json={"loyaltyActions": []},
        headers={"set-cookie": "SESSION=abc123; Path=/; HttpOnly; Secure"},
    )
