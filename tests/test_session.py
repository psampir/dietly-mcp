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
from conftest import CSRF_REJECTION_BODY, CSRF_TOKEN, LOGIN_URL, csrf_cookie_header

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
    CsrfTokenError,
    InvalidCredentialsError,
    MissingCredentialsError,
    MissingSessionError,
    NetworkError,
    SessionExpiredError,
)
from dietly_mcp.session import (
    Session,
    api_headers,
    body_snippet,
    browser_headers,
    check_response,
    has_csrf_cookie,
    is_spring_csrf_rejection,
    looks_like_cloudflare,
)


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


def _login_requests(seen: list[httpx.Request]) -> list[httpx.Request]:
    """Return only the requests that hit the login endpoint."""
    return [r for r in seen if r.url.path == "/api/auth/login"]


def test_login_primes_csrf_then_posts_with_the_header(
    settings: Settings, login_ok: None, seen: list[httpx.Request], transport
) -> None:
    """The real handshake: GET for the XSRF cookie, POST echoing it back."""
    Session.login(CREDENTIALS, settings, transport=transport)

    requests = _login_requests(seen)
    assert [r.method for r in requests] == ["GET", "POST"]
    post = requests[1]
    assert post.headers["x-xsrf-token"] == CSRF_TOKEN
    assert post.headers["origin"] == "https://panel.dietly.pl"
    assert post.headers["referer"] == "https://panel.dietly.pl/"
    # Spring compares the header against the cookie, so both must be present.
    assert f"XSRF-TOKEN={CSRF_TOKEN}" in post.headers.get("cookie", "")


def test_login_success_persists_cookie_jar_privately(
    settings: Settings, login_ok: None, transport: httpx.MockTransport
) -> None:
    session = Session.login(CREDENTIALS, settings, transport=transport)

    assert session.email == "user@example.com"
    path = Path(settings.session_path)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["email"] == "user@example.com"
    names = {cookie["name"] for cookie in stored["cookies"]}
    assert {"SESSION", "XSRF-TOKEN"} <= names
    # The password must never be written to disk.
    assert "secret" not in path.read_text(encoding="utf-8")


def test_login_sends_form_encoded_credentials(
    settings: Settings,
    login_ok: None,
    seen: list[httpx.Request],
    transport: httpx.MockTransport,
) -> None:
    Session.login(CREDENTIALS, settings, transport=transport)

    request = _login_requests(seen)[1]
    assert request.url == httpx.URL(settings.login_url)
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["x-launcher-type"] == "BROWSER_DIETLY"
    assert b"username=user%40example.com" in request.content
    assert b"password=secret" in request.content


def test_login_403_csrf_rejection_is_not_reported_as_bad_credentials(
    settings: Settings,
    prime_csrf,
    responses: dict[str, httpx.Response],
    transport: httpx.MockTransport,
) -> None:
    """The live failure mode: 403 + Spring JSON + a freshly rotated XSRF cookie."""
    prime_csrf()
    responses["POST /api/auth/login"] = httpx.Response(
        403,
        json=CSRF_REJECTION_BODY,
        headers={"server": "cloudflare", "set-cookie": csrf_cookie_header("rotated")},
    )

    with pytest.raises(CsrfTokenError) as excinfo:
        Session.login(CREDENTIALS, settings, transport=transport)

    message = str(excinfo.value)
    assert "CSRF" in message
    assert "X-XSRF-TOKEN" in message
    assert "Forbidden" in message  # the backend body is quoted back
    assert not Path(settings.session_path).exists()


def test_login_recovers_when_spring_rotates_the_csrf_token(
    settings: Settings,
    prime_csrf,
    responses: dict[str, httpx.Response],
    seen: list[httpx.Request],
    transport: httpx.MockTransport,
) -> None:
    """A rotation on the first POST triggers exactly one re-prime + retry."""
    prime_csrf("stale")
    rejection = httpx.Response(
        403, json=CSRF_REJECTION_BODY, headers={"set-cookie": csrf_cookie_header("fresh")}
    )
    success = httpx.Response(200, headers={"set-cookie": "SESSION=abc123; Path=/"})

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(401, headers={"set-cookie": csrf_cookie_header("fresh")})
        # First POST: rejected and rotated; second POST: accepted.
        posts = [r for r in seen if r.method == "POST"]
        return rejection if len(posts) == 1 else success

    session = Session.login(CREDENTIALS, settings, transport=httpx.MockTransport(handler))

    assert session.email == "user@example.com"
    assert [r.method for r in seen] == ["GET", "POST", "GET", "POST"]
    assert seen[-1].headers["x-xsrf-token"] == "fresh"


def test_login_rejected_credentials_raise_clear_error(
    settings: Settings,
    prime_csrf,
    responses: dict[str, httpx.Response],
    transport: httpx.MockTransport,
) -> None:
    """Wrong password → 401 with a plain-text Polish body (observed live)."""
    prime_csrf()
    responses["POST /api/auth/login"] = httpx.Response(
        401, text="Niepoprawne dane uwierzytelniające"
    )

    with pytest.raises(InvalidCredentialsError) as excinfo:
        Session.login(CREDENTIALS, settings, transport=transport)

    message = str(excinfo.value)
    assert "DIETLY_EMAIL" in message
    assert "401" in message
    assert not Path(settings.session_path).exists()


def test_login_cloudflare_challenge_is_reported_as_such(
    settings: Settings,
    prime_csrf,
    responses: dict[str, httpx.Response],
    transport: httpx.MockTransport,
) -> None:
    prime_csrf()
    responses["POST /api/auth/login"] = httpx.Response(
        403,
        headers={"server": "cloudflare", "cf-ray": "8f00-XXX", "cf-mitigated": "challenge"},
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
    login_ok: None,
    responses: dict[str, httpx.Response],
    seen: list[httpx.Request],
    transport: httpx.MockTransport,
) -> None:
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
    # The CSRF cookie came back from the saved jar and is echoed on reads too.
    assert seen[0].headers["x-xsrf-token"] == CSRF_TOKEN


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
    request = httpx.Request("GET", "https://panel.dietly.pl/api/company/customer/order/1")
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


def test_403_with_spring_json_is_not_treated_as_cloudflare() -> None:
    """Regression: every live response carries ``server: cloudflare``.

    Only ``cf-mitigated`` or an HTML interstitial may be read as a challenge.
    """
    response = _response(
        403, headers={"server": "cloudflare", "cf-ray": "a4b7-waw"}, json=CSRF_REJECTION_BODY
    )

    assert looks_like_cloudflare(response) is False
    assert has_csrf_cookie(response) is False


def test_looks_like_cloudflare_needs_a_real_challenge_marker() -> None:
    mitigated = _response(403, headers={"cf-mitigated": "challenge"}, json={})
    interstitial = _response(403, headers={}, html="<html>Just a moment…</html>")
    plain_json = _response(403, headers={"server": "cloudflare"}, json=CSRF_REJECTION_BODY)

    assert looks_like_cloudflare(mitigated) is True
    assert looks_like_cloudflare(interstitial) is True
    assert looks_like_cloudflare(plain_json) is False


def test_check_response_403_with_rotated_csrf_cookie_is_a_csrf_error() -> None:
    response = _response(
        403, json=CSRF_REJECTION_BODY, headers={"set-cookie": csrf_cookie_header("rotated")}
    )

    assert is_spring_csrf_rejection(response) is True

    with pytest.raises(CsrfTokenError) as excinfo:
        check_response(response)

    assert "CSRF" in str(excinfo.value)
    assert "/auth/login" in str(excinfo.value)


def test_check_response_app_level_403_is_a_session_problem_with_the_backend_body() -> None:
    response = _response(403, json={"detail": "You don't have permission"})

    with pytest.raises(SessionExpiredError) as excinfo:
        check_response(response)

    assert "dietly-mcp login" in str(excinfo.value)
    assert "You don't have permission" in str(excinfo.value)


def test_check_response_401_says_not_logged_in() -> None:
    with pytest.raises(SessionExpiredError) as excinfo:
        check_response(_response(401, json={"status": 401, "error": "Unauthorized"}))

    assert "not logged in" in str(excinfo.value)


def test_body_snippet_is_single_line_and_bounded() -> None:
    response = _response(400, text="line one\nline two   spaced")

    snippet = body_snippet(response, limit=10)
    assert snippet == "line one l"
    assert body_snippet(_response(204, text="")) == ""


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #
def test_api_headers_add_company_and_csrf_only_when_given(settings: Settings) -> None:
    bare = api_headers(settings)
    assert "company-id" not in bare
    assert "x-xsrf-token" not in bare

    full = api_headers(settings, "wybormenu", csrf_token=CSRF_TOKEN)
    assert full["company-id"] == "wybormenu"
    assert full["x-xsrf-token"] == CSRF_TOKEN


def test_browser_headers_derive_origin_from_the_login_url(settings: Settings) -> None:
    headers = browser_headers(settings, LOGIN_URL, csrf_token=CSRF_TOKEN)

    assert headers["origin"] == "https://panel.dietly.pl"
    assert headers["referer"] == "https://panel.dietly.pl/"
    assert headers["x-xsrf-token"] == CSRF_TOKEN


def test_session_csrf_token_reads_the_cookie_jar(settings: Settings, transport) -> None:
    session = Session(Session._new_client(settings, transport=transport), settings)
    assert session.csrf_token() is None

    session.http.cookies.set("XSRF-TOKEN", CSRF_TOKEN, domain="panel.dietly.pl", path="/")
    assert session.csrf_token() == CSRF_TOKEN


# --------------------------------------------------------------------------- #
# Transport: IPv4 pinning, timeouts, retries
# --------------------------------------------------------------------------- #
def test_new_client_pins_ipv4_and_sets_explicit_timeouts(settings: Settings) -> None:
    client = Session._new_client(settings)
    try:
        assert client.timeout.connect == settings.connect_timeout
        assert client.timeout.read == settings.timeout
        assert client.follow_redirects is True
        # httpx keeps the bound local address on its connection pool.
        assert client._transport._pool._local_address == "0.0.0.0"  # noqa: SLF001
    finally:
        client.close()


def test_new_client_can_opt_out_of_ipv4_pinning(settings: Settings) -> None:
    from dataclasses import replace

    client = Session._new_client(replace(settings, ipv4_only=False))
    try:
        assert client._transport._pool._local_address is None  # noqa: SLF001
    finally:
        client.close()


def test_send_with_retries_recovers_from_a_transient_error() -> None:
    from dietly_mcp.http import send_with_retries

    attempts: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        response = send_with_retries(
            client, "GET", "https://panel.dietly.pl/api/profile",
            retries=2, sleeper=slept.append,
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert len(attempts) == 2
    assert slept == [0.4]  # one backoff before the successful retry


def test_send_with_retries_gives_up_and_reraises() -> None:
    from dietly_mcp.http import send_with_retries

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.ConnectError):
            send_with_retries(
                client, "GET", "https://panel.dietly.pl/api/profile",
                retries=1, sleeper=lambda _seconds: None,
            )
    finally:
        client.close()


def test_build_transport_ipv4_pinning_is_toggleable() -> None:
    from dietly_mcp.http import build_transport

    assert build_transport()._pool._local_address == "0.0.0.0"  # noqa: SLF001
    assert build_transport(ipv4_only=False)._pool._local_address is None  # noqa: SLF001


def test_settings_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from dietly_mcp.config import load_settings

    monkeypatch.setenv("DIETLY_TIMEOUT", "12.5")
    monkeypatch.setenv("DIETLY_COMPANY_ID", "innykatering")
    settings = load_settings()

    assert settings.timeout == 12.5
    assert settings.company_id == "innykatering"


def test_settings_transport_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from dietly_mcp.config import load_settings

    monkeypatch.setenv("DIETLY_CONNECT_TIMEOUT", "3.5")
    monkeypatch.setenv("DIETLY_RETRIES", "5")
    monkeypatch.setenv("DIETLY_IPV4_ONLY", "no")
    settings = load_settings()

    assert settings.connect_timeout == 3.5
    assert settings.retries == 5
    assert settings.ipv4_only is False


def test_settings_edit_cutoff_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from dietly_mcp.config import DEFAULT_EDIT_CUTOFF_HOURS, load_settings

    assert load_settings().edit_cutoff_hours == DEFAULT_EDIT_CUTOFF_HOURS == 36.0

    monkeypatch.setenv("DIETLY_EDIT_CUTOFF_HOURS", "12.5")
    assert load_settings().edit_cutoff_hours == 12.5


def test_settings_defaults_target_the_panel_host() -> None:
    from dietly_mcp.config import Settings as FreshSettings

    settings = FreshSettings()
    assert settings.base_url == "https://panel.dietly.pl"
    assert settings.login_url.startswith(settings.base_url)
    assert settings.ipv4_only is True
    assert settings.retries == 2


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
    settings: Settings, login_ok: None, transport: httpx.MockTransport
) -> None:
    Session.login(CREDENTIALS, settings, transport=transport)
    mode = stat.S_IMODE(os.stat(settings.session_path).st_mode)
    assert mode == 0o600
