"""Login, cookie persistence and session validation.

Auth is a plain form login that yields an httpOnly session cookie:

1. ``POST https://panel.dietly.pl/api/auth/login`` with
   ``application/x-www-form-urlencoded`` body ``username=…&password=…``;
2. the cookie jar is persisted to ``~/.local/state/dietly-mcp/session.json``
   (directory ``700``, file ``600``);
3. ``GET /api/profile`` verifies that the session is still valid.

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
    InvalidCredentialsError,
    MissingSessionError,
    NetworkError,
    SessionExpiredError,
)

__all__ = ["Session", "api_headers", "check_response", "looks_like_cloudflare"]

#: Substrings that betray a Cloudflare interstitial / block page.
_CLOUDFLARE_HINTS = (
    "just a moment",
    "checking your browser",
    "cf-chl",
    "attention required",
    "cloudflare",
    "enable javascript and cookies to continue",
)


def api_headers(settings: Settings, company_id: str | None = None) -> dict[str, str]:
    """Return the headers every Dietly API call needs.

    Args:
        settings: runtime settings (user agent, language).
        company_id: catering slug; omit for account-wide calls.
    """
    headers = {
        "accept": "application/json",
        "accept-language": settings.accept_language,
        "x-launcher-type": "BROWSER_DIETLY",
        "user-agent": settings.user_agent,
    }
    if company_id:
        headers["company-id"] = company_id
    return headers


def looks_like_cloudflare(response: httpx.Response) -> bool:
    """Guess whether a response is a Cloudflare challenge instead of real JSON."""
    if response.headers.get("cf-mitigated"):
        return True
    if "cloudflare" in (response.headers.get("server") or "").lower():
        return True
    if response.headers.get("cf-ray") and response.status_code in (403, 429, 503):
        # A cf-ray header alone is normal; it only signals a challenge together
        # with a failure status and a non-JSON body.
        content_type = (response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            return True
    try:
        body = response.text[:4000].lower()
    except Exception:  # pragma: no cover - decoding failure
        return False
    return any(hint in body for hint in _CLOUDFLARE_HINTS)


def _cloudflare_message(url: str | None) -> str:
    return (
        "Cloudflare blocked the request" + (f" to {url}" if url else "") + ".\n"
        "Dietly sits behind Cloudflare, which sometimes challenges non-browser\n"
        "clients (HTTP 403/503 or a 'Just a moment…' page). Try again in a minute;\n"
        "if it keeps happening, a curl_cffi (browser TLS fingerprint) fallback may\n"
        "need to be added in a later phase."
    )


def check_response(response: httpx.Response) -> None:
    """Raise a specific :class:`~dietly_mcp.errors.DietlyError` for known failures.

    Only raises; successful responses pass through untouched.
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

    if status in (401, 403):
        raise SessionExpiredError(
            "Dietly rejected the stored session (HTTP "
            f"{status}). Run `dietly-mcp login` to log in again.",
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

    raise ApiError(
        f"Dietly returned HTTP {status} for {url or 'the request'}.",
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
        return httpx.Client(
            headers={
                "user-agent": settings.user_agent,
                "accept-language": settings.accept_language,
            },
            timeout=settings.timeout,
            follow_redirects=True,
            transport=transport,
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

        Raises:
            InvalidCredentialsError: the login endpoint replied 401/403.
            CloudflareBlockedError: Cloudflare challenged the login request.
            NetworkError: the request could not be sent.
        """
        http = cls._new_client(settings, transport=transport)
        try:
            response = http.post(
                settings.login_url,
                data={"username": credentials.username, "password": credentials.password},
                headers=api_headers(settings),
            )
        except httpx.HTTPError as exc:
            http.close()
            raise NetworkError(
                f"Could not reach {settings.login_url}: {exc}. Check your network connection."
            ) from exc

        if response.status_code in (401, 403) and not looks_like_cloudflare(response):
            http.close()
            raise InvalidCredentialsError(
                "Login failed (HTTP "
                f"{response.status_code}). Check DIETLY_EMAIL and DIETLY_PASSWORD "
                f"in {settings.credentials_path} (or the DIETLY_* environment variables)."
            )
        check_response(response)

        session = cls(http, settings, email=credentials.username)
        session.harmonise_cookie_domains()
        session.save()
        return session

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

        Login happens on ``panel.dietly.pl`` while the API lives on ``dietly.pl``.
        Dietly normally sets a ``Domain=.dietly.pl`` cookie, so this is a no-op;
        but if the server ever sets a host-only cookie for the login host, the
        whole session would be useless. In that case (and only when no jar cookie
        would reach the API host) every cookie is re-scoped to the API host.
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
            response = self.http.get(url, headers=api_headers(self.settings))
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
