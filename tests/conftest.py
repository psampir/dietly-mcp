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

LOGIN_URL = "https://panel.dietly.pl/api/auth/login"
BASE_URL = "https://dietly.pl"


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
        "EMAIL",
        "USERNAME",
        "PASSWORD",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at throwaway config/state paths under ``tmp_path``."""
    return Settings(
        base_url=BASE_URL,
        login_url=LOGIN_URL,
        company_id="wybormenu",
        user_agent="dietly-mcp-test/0.1",
        accept_language="pl-PL,pl;q=0.9",
        timeout=5.0,
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
