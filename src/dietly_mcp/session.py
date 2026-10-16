"""Login, cookie persistence and session validation.

Auth is a plain form login guarded by a Spring Security CSRF token. The real
browser sequence — verified against the live SPA
(``panel.dietly.pl/assets/index-*.js``) — is:

1. ``GET https://panel.dietly.pl/api/auth/login`` answers ``401`` **and sets the
   ``XSRF-TOKEN`` cookie**. Any ``/api/**`` request does this; it is how the SPA
   obtains the token.
2. ``POST https://panel.dietly.pl/api/auth/login`` with
   ``application/x-www-form-urlencoded`` body ``username=…&password=…`` and the
   header ``X-XSRF-TOKEN: <value of the XSRF-TOKEN cookie>``.
   Response ``200`` sets ``SESSION`` (host-only for ``panel.dietly.pl``),
   ``remember-me`` and a rotated ``XSRF-TOKEN``.

Skipping step 2's header makes Spring answer ``403`` with
``{"status":403,"error":"Forbidden","path":"/auth/login"}`` regardless of
whether the password is right — it is a CSRF rejection, not bad credentials and
not a Cloudflare challenge.

The cookie jar is persisted to ``~/.local/state/dietly-mcp/session.json``
(directory ``700``, file ``600``); ``GET /api/profile`` verifies it.

Nothing in this module ever logs credentials.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .config import (
    Credentials,
    Settings,
    ensure_private_dir,
    ensure_private_file,
)
from .errors import (
    ApiError,
    CateringMismatchError,
    CloudflareBlockedError,
    ConfigError,
    CsrfTokenError,
    InvalidCredentialsError,
    MissingSessionError,
    NetworkError,
    SessionExpiredError,
)
from .http import build_timeout, build_transport, send_with_retries

__all__ = [
    "Session",
    "api_headers",
    "body_snippet",
    "check_response",
    "has_csrf_cookie",
    "is_spring_csrf_rejection",
    "looks_like_cloudflare",
]

#: Substrings that betray a Cloudflare interstitial / challenge page.
_CLOUDFLARE_HTML_HINTS = (
    "just a moment",
    "checking your browser",
    "cf-chl",
    "attention required",
    "enable javascript and cookies to continue",
    "cf-error",
    "cloudflare ray id",
)

#: How much of a backend error body to quote back to the user.
_SNIPPET_LIMIT = 400


def api_headers(
    settings: Settings,
    company_id: str | None = None,
    *,
    csrf_token: str | None = None,
) -> dict[str, str]:
    """Return the headers every Dietly API call needs.

    Args:
        settings: runtime settings (user agent, language).
        company_id: catering slug; omit for account-wide calls.
        csrf_token: value of the ``XSRF-TOKEN`` cookie. Sent as ``X-XSRF-TOKEN``
            so Spring accepts state-changing requests. Harmless on reads.
    """
    headers = {
        "accept": "application/json",
        "accept-language": settings.accept_language,
        "x-launcher-type": "BROWSER_DIETLY",
        "user-agent": settings.user_agent,
    }
    if company_id:
        headers["company-id"] = company_id
    if csrf_token:
        headers["x-xsrf-token"] = csrf_token
    return headers


def browser_headers(
    settings: Settings, url: str, *, csrf_token: str | None = None
) -> dict[str, str]:
    """Return headers for a login-page request (adds Origin/Referer).

    The SPA issues its login POST from ``panel.dietly.pl``, so the request
    carries that origin. Both are derived from ``url`` to stay correct when a
    different login host is configured.

    Args:
        settings: runtime settings.
        url: the login URL the headers are for.
        csrf_token: optional CSRF token to echo back.
    """
    parsed = httpx.URL(url)
    origin = f"{parsed.scheme}://{parsed.host}"
    if parsed.port is not None:
        origin += f":{parsed.port}"
    headers = api_headers(settings, csrf_token=csrf_token)
    headers["origin"] = origin
    headers["referer"] = origin + "/"
    return headers


def has_csrf_cookie(response: httpx.Response) -> bool:
    """Whether a response hands out (or rotates) an ``XSRF-TOKEN`` cookie."""
    return any(
        raw.split("=", 1)[0].strip().lower() == "xsrf-token"
        for raw in response.headers.get_list("set-cookie")
    )


def body_snippet(response: httpx.Response, limit: int = _SNIPPET_LIMIT) -> str:
    """Return a short, single-line, secret-free quote of a response body.

    Dietly's Spring backend answers API errors with a small JSON document
    (``{"status":403,"error":"Forbidden","path":"/auth/login"}``); quoting it is
    the fastest way to tell a CSRF rejection from a real permission error.
    """
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - undecodable body
        return ""
    text = " ".join(text.split())
    if not text:
        return ""
    content_type = (response.headers.get("content-type") or "").lower()
    if "json" in content_type:
        try:
            return json.dumps(response.json(), ensure_ascii=False)[:limit]
        except ValueError:
            pass
    return text[:limit]


def looks_like_cloudflare(response: httpx.Response) -> bool:
    """Guess whether a response is a Cloudflare challenge instead of real JSON.

    Only two things are trusted:

    * the ``cf-mitigated`` response header, which Cloudflare adds when it
      deliberately challenged or blocked the request;
    * an HTML body carrying one of Cloudflare's interstitial markers.

    A plain ``server: cloudflare`` header is **not** evidence — Cloudflare
    fronts Dietly on *every* response, including ordinary ``401``/``403`` JSON
    errors from the Spring backend.
    """
    if response.headers.get("cf-mitigated"):
        return True
    content_type = (response.headers.get("content-type") or "").lower()
    try:
        body = response.text[:4000].lower()
    except Exception:  # pragma: no cover - decoding failure
        return False
    if "html" in content_type:
        return any(hint in body for hint in _CLOUDFLARE_HTML_HINTS)
    return "cf-chl" in body


def is_spring_csrf_rejection(response: httpx.Response) -> bool:
    """Whether ``response`` is Spring Security refusing a state-changing call.

    Two independent signals, either is enough:

    * the body names CSRF explicitly;
    * the response rotates the ``XSRF-TOKEN`` cookie while answering ``403`` —
      Spring's way of saying "here is the token you forgot to send".

    Cloudflare challenges are excluded first, so an app-level ``403`` (Dietly's
    own JSON) is never mistaken for a browser check.
    """
    if response.status_code != 403 or looks_like_cloudflare(response):
        return False
    if has_csrf_cookie(response):
        return True
    return "csrf" in body_snippet(response).lower()


def _cloudflare_message(url: str | None) -> str:
    return (
        "Cloudflare blocked the request" + (f" to {url}" if url else "") + ".\n"
        "Dietly sits behind Cloudflare, which sometimes challenges non-browser\n"
        "clients (HTTP 403/503 or a 'Just a moment…' page). Try again in a minute;\n"
        "if it keeps happening, a curl_cffi (browser TLS fingerprint) fallback may\n"
        "need to be added in a later phase."
    )


def _csrf_message(url: str | None, snippet: str) -> str:
    return (
        "Dietly rejected the request for a missing/invalid CSRF token"
        + (f" ({url})" if url else "")
        + ".\n"
        "The panel is a Spring app: login needs the XSRF-TOKEN cookie from a\n"
        "GET, echoed in the X-XSRF-TOKEN header. `dietly-mcp login` does this\n"
        "automatically, so a persistent failure means the app changed again.\n"
        f"Backend said: {snippet or '(empty body)'}"
    )


def _session_message(status: int, url: str | None, snippet: str) -> str:
    reason = "not logged in" if status == 401 else "rejected"
    return (
        f"Dietly {reason} the stored session (HTTP {status}"
        + (f" for {url}" if url else "")
        + "). Run `dietly-mcp login` to log in again."
        + (f"\nBackend said: {snippet}" if snippet else "")
    )


def check_response(response: httpx.Response) -> None:
    """Raise a specific :class:`~dietly_mcp.errors.DietlyError` for known failures.

    Only raises; successful responses pass through untouched.

    Classification order matters: a Cloudflare challenge is detected by its own
    markers (never by ``server: cloudflare`` alone), a Spring CSRF rejection
    before the generic ``403`` branch, and ``401``/``403`` from the app become a
    :class:`SessionExpiredError` carrying the backend's JSON snippet.
    """
    status = response.status_code
    url = str(response.request.url) if response.request is not None else None
    if status < 400:
        return

    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        payload = None

    if status == 490:
        raise CateringMismatchError(
            "HTTP 490 — this order belongs to a different catering slug.\n"
            "Dietly rejects order-scoped calls when the company-id header does not\n"
            "match the order's catering. Re-run with the right --company-id / let the\n"
            "client read it from the active-orders list.",
            status_code=status,
            url=url,
            payload=payload,
        )

    if status in (403, 503) and looks_like_cloudflare(response):
        raise CloudflareBlockedError(
            _cloudflare_message(url), status_code=status, url=url, payload=payload
        )

    if is_spring_csrf_rejection(response):
        raise CsrfTokenError(
            _csrf_message(url, body_snippet(response)),
            status_code=status,
            url=url,
            payload=payload,
        )

    if status in (401, 403):
        raise SessionExpiredError(
            _session_message(status, url, body_snippet(response)),
            status_code=status,
            url=url,
            payload=payload,
        )

    if status == 429:
        raise ApiError(
            "Dietly is rate limiting us (HTTP 429). Wait a moment and retry.",
            status_code=status,
            url=url,
            payload=payload,
        )

    snippet = body_snippet(response)
    raise ApiError(
        f"Dietly returned HTTP {status} for {url or 'the request'}."
        + (f"\nBackend said: {snippet}" if snippet else ""),
        status_code=status,
        url=url,
        payload=payload,
    )


def _cookie_applies_to(cookie: Any, host: str) -> bool:
    """Whether a jar cookie would be sent to ``host``."""
    domain = str(getattr(cookie, "domain", "") or "").lstrip(".").lower()
    host = host.lower()
    if not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _profile_email(profile: Any) -> str | None:
    """Best-effort extraction of the account email from a ``/api/profile`` body."""
    keys = ("email", "mail", "login", "userName", "username")
    if not isinstance(profile, dict):
        return None
    for key in keys:
        value = profile.get(key)
        if isinstance(value, str) and "@" in value:
            return value
    for nested in ("data", "user", "profile", "account", "customer"):
        child = profile.get(nested)
        if isinstance(child, dict):
            found = _profile_email(child)
            if found:
                return found
    return None


class Session:
    """An authenticated Dietly HTTP session backed by an ``httpx.Client``."""

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        *,
        email: str | None = None,
    ) -> None:
        self.http = http
        self.settings = settings
        self.email = email

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_client(
        settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> httpx.Client:
        """Build the HTTP client: IPv4-pinned, with explicit timeouts.

        When ``transport`` is omitted (the normal case) the transport is pinned
        to IPv4 because broken IPv6 routes on some hosts cause instant
        ``ConnectError``s or long read timeouts.
        """
        return httpx.Client(
            headers={
                "user-agent": settings.user_agent,
                "accept-language": settings.accept_language,
            },
            timeout=build_timeout(read=settings.timeout, connect=settings.connect_timeout),
            follow_redirects=True,
            transport=transport
            if transport is not None
            else build_transport(ipv4_only=settings.ipv4_only),
        )

    # ------------------------------------------------------------------ #
    # CSRF (Spring Security cookie token)
    # ------------------------------------------------------------------ #
    def csrf_token(self) -> str | None:
        """Return the current ``XSRF-TOKEN`` cookie value, if the jar has one.

        This is a CSRF token bound to this cookie jar, not an account secret:
        it is safe to send in a header but is still never printed.
        """
        return self.http.cookies.get("XSRF-TOKEN")

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request with the configured retry policy."""
        return send_with_retries(
            self.http, method, url, retries=self.settings.retries, **kwargs
        )

    def prime_csrf(self, *, force: bool = False) -> str | None:
        """Obtain a fresh ``XSRF-TOKEN`` cookie and return its value.

        Any ``/api/**`` request makes Spring hand out the token; the login
        endpoint is used because it is guaranteed to exist and answers ``401``
        without touching the account.

        Args:
            force: ignore an existing cookie and fetch a new one (used when the
                server rejected a token we already had).
        """
        if not force:
            existing = self.csrf_token()
            if existing:
                return existing
        self._request("GET", self.settings.login_url, headers=api_headers(self.settings))
        return self.csrf_token()

    def _post_login(
        self, credentials: Credentials, settings: Settings, csrf_token: str | None
    ) -> httpx.Response:
        """POST the credentials, echoing the CSRF token in ``X-XSRF-TOKEN``."""
        return self._request(
            "POST",
            settings.login_url,
            data={"username": credentials.username, "password": credentials.password},
            headers={
                **browser_headers(settings, settings.login_url, csrf_token=csrf_token),
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    @classmethod
    def login(
        cls,
        credentials: Credentials,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Session:
        """Log in with ``credentials`` and persist the resulting cookie jar.

        Sequence: prime the CSRF cookie with a GET, then POST the form with
        ``X-XSRF-TOKEN``. If Spring still rejects the token (it rotates it on
        some deployments) the token is re-primed once and the POST retried.

        Raises:
            InvalidCredentialsError: the login endpoint replied 401.
            CsrfTokenError: Spring rejected the CSRF token even after a retry.
            CloudflareBlockedError: Cloudflare challenged the login request.
            NetworkError: the request could not be sent.
        """
        session = cls(cls._new_client(settings, transport=transport), settings)
        http = session.http
        try:
            csrf = session.prime_csrf()
            response = session._post_login(credentials, settings, csrf)
            if is_spring_csrf_rejection(response):
                # Spring rotated the cookie we were holding: re-prime once.
                csrf = session.prime_csrf(force=True)
                response = session._post_login(credentials, settings, csrf)
        except httpx.HTTPError as exc:
            http.close()
            raise NetworkError(
                f"Could not reach {settings.login_url}: {exc}. Check your network connection."
            ) from exc

        session._raise_login_failure(response)
        session.email = credentials.username
        session.harmonise_cookie_domains()
        session.save()
        return session

    @staticmethod
    def _raise_login_failure(response: httpx.Response) -> None:
        """Translate a failed login response into the right exception.

        ``401`` means the email/password pair was refused. A ``403`` that is not
        a Cloudflare challenge and not a CSRF rejection is Dietly refusing the
        request for another reason, so the backend body is surfaced verbatim.
        """
        status = response.status_code
        if status < 400:
            return
        url = str(response.request.url) if response.request is not None else None
        snippet = body_snippet(response)
        if looks_like_cloudflare(response):
            raise CloudflareBlockedError(
                _cloudflare_message(url), status_code=status, url=url, payload=None
            )
        if is_spring_csrf_rejection(response):
            raise CsrfTokenError(
                _csrf_message(url, snippet), status_code=status, url=url, payload=None
            )
        if status == 401:
            raise InvalidCredentialsError(
                "Login failed: Dietly refused the email/password (HTTP 401).\n"
                "Check DIETLY_EMAIL and DIETLY_PASSWORD (or the DIETLY_* environment\n"
                "variables) — the credentials file is at the configured path.\n"
                f"Backend said: {snippet or '(empty body)'}",
                status_code=status,
                url=url,
                payload=None,
            )
        check_response(response)

    @classmethod
    def load(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Session:
        """Load a persisted session.

        Raises:
            MissingSessionError: no session file exists yet.
        """
        path = settings.session_path
        if not path.exists():
            raise MissingSessionError(f"No stored session at {path}. Run `dietly-mcp login` first.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MissingSessionError(
                f"Stored session at {path} is unreadable ({exc}). "
                "Run `dietly-mcp login` to recreate it."
            ) from exc

        http = cls._new_client(settings, transport=transport)
        for cookie in data.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            if not name:
                continue
            http.cookies.set(
                str(name),
                "" if value is None else str(value),
                domain=str(cookie.get("domain") or ""),
                path=str(cookie.get("path") or "/"),
            )
        return cls(http, settings, email=data.get("email"))

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def harmonise_cookie_domains(self) -> None:
        """Re-scope cookies to the API host when none of them would be sent there.

        By default the login host and the API host are the same
        (``panel.dietly.pl``), and the server's cookies already match, so this is
        a no-op. It matters only when ``DIETLY_BASE_URL`` points somewhere else:
        Dietly sets a host-only ``SESSION`` cookie for the login host, which
        would make the whole session useless. In that case (and only when no jar
        cookie would reach the API host) every cookie is re-scoped to that host.
        """
        host = httpx.URL(self.settings.base_url).host
        cookies = list(self.http.cookies.jar)
        if not cookies or any(_cookie_applies_to(c, host) for c in cookies):
            return
        saved = [(c.name, c.value, c.path or "/") for c in cookies]
        self.http.cookies.clear()
        for name, value, path in saved:
            self.http.cookies.set(name, value, domain=host, path=path)

    def cookies_as_dicts(self) -> list[dict[str, Any]]:
        """Serialise the current cookie jar to plain dicts."""
        jar: list[dict[str, Any]] = []
        for cookie in self.http.cookies.jar:
            jar.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                }
            )
        return jar

    def save(self) -> Path:
        """Write the cookie jar to ``settings.session_path`` (0600)."""
        path = Path(self.settings.session_path)
        ensure_private_dir(path.parent)
        payload = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "email": self.email,
            "cookies": self.cookies_as_dicts(),
        }
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            ensure_private_file(tmp_path)
            os.replace(tmp_path, path)
        except OSError as exc:
            raise ConfigError(f"Could not write the session file {path}: {exc}") from exc
        ensure_private_file(path)
        return path

    def clear(self) -> bool:
        """Delete the persisted session file. Returns ``True`` if one existed."""
        path = Path(self.settings.session_path)
        existed = path.exists()
        if existed:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - permissions
                raise ConfigError(f"Could not delete {path}: {exc}") from exc
        self.http.cookies.clear()
        return existed

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self.http.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def verify(self) -> dict[str, Any]:
        """Fetch ``/api/profile`` and return its parsed body.

        Raises:
            SessionExpiredError: the session cookie is no longer accepted.
        """
        url = f"{self.settings.base_url}/api/profile"
        try:
            response = self._request(
                "GET", url, headers=api_headers(self.settings, csrf_token=self.csrf_token())
            )
        except httpx.HTTPError as exc:
            raise NetworkError(f"Could not reach {url}: {exc}.") from exc
        check_response(response)
        try:
            profile = response.json()
        except ValueError as exc:
            raise ApiError(
                f"{url} did not return JSON (unexpected page or challenge).",
                status_code=response.status_code,
                url=url,
            ) from exc
        if isinstance(profile, dict):
            found = _profile_email(profile)
            if found:
                self.email = found
        return profile if isinstance(profile, dict) else {"data": profile}

    def is_valid(self) -> bool:
        """Return ``True`` when the session still works.

        A rejected session returns ``False``; network problems are raised
        instead (they are not the session's fault). The email is refreshed as a
        side effect when the API exposes it.
        """
        try:
            self.verify()
        except SessionExpiredError:
            return False
        return True
