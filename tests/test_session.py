"""Tests for credentials loading, login, session persistence and validation.

Everything runs against ``httpx.MockTransport``; no network access.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from dietly_mcp.config import (
    Credentials,
    Settings,
    insecure_permissions_warning,
    load_credentials,
    parse_env_file,
)
from dietly_mcp.errors import (
    CateringMismatchError,
    CloudflareBlockedError,
    InvalidCredentialsError,
    MissingCredentialsError,
    MissingSessionError,
    NetworkError,
    SessionExpiredError,
)
from dietly_mcp.session import Session, check_response


# --------------------------------------------------------------------------- #
# Credentials file parsing
# --------------------------------------------------------------------------- #
def test_parse_env_file_handles_comments_quotes_and_export() -> None:
    text = """
    # a comment
    DIETLY_EMAIL=user@example.com

    DIETLY_PASSWORD='se=cret with spaces'
    export DIETLY_COMPANY_ID="wybormenu"
    NOT_A_PAIR
    """
    values = parse_env_file(text)
    assert values["DIETLY_EMAIL"] == "user@example.com"
    assert values["DIETLY_PASSWORD"] == "se=cret with spaces"
    assert values["DIETLY_COMPANY_ID"] == "wybormenu"
    assert "NOT_A_PAIR" not in values


def test_load_credentials_from_file(settings: Settings) -> None:
    settings.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.credentials_path.write_text(
        "DIETLY_EMAIL=from-file@example.com\nDIETLY_PASSWORD=hunter2\n", encoding="utf-8"
    )

    credentials = load_credentials(settings.credentials_path)

    assert credentials.username == "from-file@example.com"
    assert credentials.password == "hunter2"
    assert credentials.email() == "from-file@example.com"
    assert credentials.source == str(settings.credentials_path)


def test_load_credentials_accepts_bare_key_names(settings: Settings) -> None:
    settings.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.credentials_path.write_text("EMAIL=a@b.pl\nPASSWORD=pw\n", encoding="utf-8")
    credentials = load_credentials(settings.credentials_path)
    assert (credentials.username, credentials.password) == ("a@b.pl", "pw")


def test_load_credentials_falls_back_to_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIETLY_EMAIL", "env@example.com")
    monkeypatch.setenv("DIETLY_PASSWORD", "env-pass")

    credentials = load_credentials(settings.credentials_path)

    assert credentials.username == "env@example.com"
    assert credentials.source == "environment"


def test_load_credentials_missing_file_gives_actionable_message(settings: Settings) -> None:
    with pytest.raises(MissingCredentialsError) as excinfo:
        load_credentials(settings.credentials_path)

    message = str(excinfo.value)
    assert "No credentials file" in message
    assert "chmod 600" in message
    assert "DIETLY_EMAIL" in message  # shows the expected format, no real secret


def test_load_credentials_warns_loudly_on_weak_permissions(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    path = settings.credentials_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DIETLY_EMAIL=a@b.pl\nDIETLY_PASSWORD=sup3r-s3cret-pass\n", encoding="utf-8")
    path.chmod(0o644)

    credentials = load_credentials(path)

    captured = capsys.readouterr()
    assert credentials.password == "sup3r-s3cret-pass"
    assert "WARNING" in captured.err
    assert "chmod 600" in captured.err
    assert "sup3r-s3cret-pass" not in captured.err  # never echo secrets
    assert "sup3r-s3cret-pass" not in captured.out
    assert insecure_permissions_warning(path) is not None


def test_load_credentials_quiet_on_600_permissions(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    path = settings.credentials_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DIETLY_EMAIL=a@b.pl\nDIETLY_PASSWORD=pw\n", encoding="utf-8")
    path.chmod(0o600)

    load_credentials(path)

    assert "WARNING" not in capsys.readouterr().err
    assert insecure_permissions_warning(path) is None


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
CREDENTIALS = Credentials(username="user@example.com", password="secret", source="test")


def test_login_success_persists_cookie_jar_privately(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(
        200, headers={"set-cookie": "SESSION=abc123; Path=/; HttpOnly; Secure"}
    )

    session = Session.login(CREDENTIALS, settings, transport=transport)

    assert session.email == "user@example.com"
    path = Path(settings.session_path)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["email"] == "user@example.com"
    assert stored["cookies"][0]["name"] == "SESSION"
    # The password must never be written to disk.
    assert "secret" not in path.read_text(encoding="utf-8")


def test_login_sends_form_encoded_credentials(
    settings: Settings,
    responses: dict[str, httpx.Response],
    seen: list[httpx.Request],
    transport: httpx.MockTransport,
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(200, headers={"set-cookie": "S=1; Path=/"})

    Session.login(CREDENTIALS, settings, transport=transport)

    request = seen[0]
    assert request.url == httpx.URL(settings.login_url)
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["x-launcher-type"] == "BROWSER_DIETLY"
    assert b"username=user%40example.com" in request.content
    assert b"password=secret" in request.content


def test_login_rejected_credentials_raise_clear_error(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(InvalidCredentialsError) as excinfo:
        Session.login(CREDENTIALS, settings, transport=transport)

    assert "DIETLY_EMAIL" in str(excinfo.value)
    assert not Path(settings.session_path).exists()


def test_login_cloudflare_challenge_is_reported_as_such(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-ray": "8f00-XXX"},
        html="<html><title>Just a moment...</title></html>",
    )

    with pytest.raises(CloudflareBlockedError) as excinfo:
        Session.login(CREDENTIALS, settings, transport=transport)

    message = str(excinfo.value)
    assert "Cloudflare" in message
    assert "curl_cffi" in message


def test_login_network_failure_is_wrapped(settings: Settings) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(NetworkError) as excinfo:
        Session.login(CREDENTIALS, settings, transport=httpx.MockTransport(boom))

    assert "Could not reach" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Session persistence / validation
# --------------------------------------------------------------------------- #
def test_load_without_session_file_asks_for_login(settings: Settings) -> None:
    with pytest.raises(MissingSessionError) as excinfo:
        Session.load(settings)

    assert "dietly-mcp login" in str(excinfo.value)


def test_load_sends_persisted_cookie(
    settings: Settings,
    responses: dict[str, httpx.Response],
    seen: list[httpx.Request],
    transport: httpx.MockTransport,
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(
        200, headers={"set-cookie": "SESSION=abc123; Path=/; HttpOnly"}
    )
    Session.login(CREDENTIALS, settings, transport=transport)
    seen.clear()

    responses["GET /api/profile"] = httpx.Response(200, json={"email": "user@example.com"})
    restored = Session.load(settings, transport=transport)
    profile = restored.verify()

    assert profile["email"] == "user@example.com"
    assert restored.email == "user@example.com"
    assert "SESSION=abc123" in seen[0].headers.get("cookie", "")
    assert seen[0].headers["accept"] == "application/json"
    assert seen[0].headers["x-launcher-type"] == "BROWSER_DIETLY"
    assert seen[0].headers["accept-language"] == "pl-PL,pl;q=0.9"


def test_verify_expired_session(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text(
        json.dumps(
            {
                "version": 1,
                "cookies": [{"name": "SESSION", "value": "x", "domain": "dietly.pl", "path": "/"}],
            }
        ),
        encoding="utf-8",
    )
    responses["GET /api/profile"] = httpx.Response(401, json={"message": "unauthorized"})

    session = Session.load(settings, transport=transport)
    with pytest.raises(SessionExpiredError) as excinfo:
        session.verify()

    assert "dietly-mcp login" in str(excinfo.value)
    assert session.is_valid() is False


def test_is_valid_true_on_healthy_session(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text(json.dumps({"version": 1, "cookies": []}), encoding="utf-8")
    responses["GET /api/profile"] = httpx.Response(
        200, json={"data": {"email": "nested@example.com"}}
    )

    session = Session.load(settings, transport=transport)

    assert session.is_valid() is True
    assert session.email == "nested@example.com"


def test_clear_removes_session_file(settings: Settings, transport: httpx.MockTransport) -> None:
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    session = Session.load(settings, transport=transport)

    assert session.clear() is True
    assert not settings.session_path.exists()
    assert session.clear() is False


def test_corrupt_session_file_is_reported(
    settings: Settings, transport: httpx.MockTransport
) -> None:
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(MissingSessionError):
        Session.load(settings, transport=transport)


# --------------------------------------------------------------------------- #
# Status-code mapping
# --------------------------------------------------------------------------- #
def _response(status: int, **kwargs: object) -> httpx.Response:
    request = httpx.Request("GET", "https://dietly.pl/api/company/customer/order/1")
    return httpx.Response(status, request=request, **kwargs)  # type: ignore[arg-type]


def test_check_response_490_maps_to_catering_mismatch() -> None:
    with pytest.raises(CateringMismatchError) as excinfo:
        check_response(_response(490, json={"message": "Zamówienie nie istnieje w tym cateringu"}))

    assert "different catering" in str(excinfo.value)
    assert excinfo.value.status_code == 490


def test_check_response_503_cloudflare() -> None:
    response = _response(503, headers={"server": "cloudflare"}, html="Checking your browser...")

    with pytest.raises(CloudflareBlockedError) as excinfo:
        check_response(response)

    assert "curl_cffi" in str(excinfo.value)


def test_check_response_passes_through_success() -> None:
    check_response(_response(200, json={"ok": True}))


def test_settings_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from dietly_mcp.config import load_settings

    monkeypatch.setenv("DIETLY_TIMEOUT", "12.5")
    monkeypatch.setenv("DIETLY_COMPANY_ID", "innykatering")
    settings = load_settings()

    assert settings.timeout == 12.5
    assert settings.company_id == "innykatering"


def test_default_paths_use_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dietly_mcp.config import (
        config_dir,
        default_credentials_path,
        default_session_path,
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))

    assert config_dir() == tmp_path / "cfg" / "dietly-mcp"
    assert default_credentials_path() == tmp_path / "cfg" / "dietly-mcp" / "credentials.env"
    assert default_session_path() == tmp_path / "st" / "dietly-mcp" / "session.json"


def test_credentials_source_is_preserved_and_never_a_secret() -> None:
    credentials = Credentials(
        username="a@b.pl", password="topsecret", source="/home/x/credentials.env"
    )
    assert credentials.source == "/home/x/credentials.env"


def test_no_password_in_session_file_permissions_check(
    settings: Settings, responses: dict[str, httpx.Response], transport: httpx.MockTransport
) -> None:
    responses["POST /api/auth/login"] = httpx.Response(200, headers={"set-cookie": "S=1; Path=/"})
    Session.login(CREDENTIALS, settings, transport=transport)
    mode = stat.S_IMODE(os.stat(settings.session_path).st_mode)
    assert mode == 0o600
