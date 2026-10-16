"""Exception hierarchy for dietly-mcp.

Every error that this package raises deliberately derives from
:class:`DietlyError`, so callers (the CLI, later the MCP server) can catch one
type and print a clean message instead of a traceback.
"""

from __future__ import annotations

from typing import Any


class DietlyError(Exception):
    """Base class for every error raised by dietly-mcp."""


# --------------------------------------------------------------------------- #
# Configuration / credentials
# --------------------------------------------------------------------------- #
class ConfigError(DietlyError):
    """Local configuration is invalid or incomplete."""


class MissingCredentialsError(ConfigError):
    """No credentials file (and no environment fallback) was found."""


# --------------------------------------------------------------------------- #
# HTTP / API
# --------------------------------------------------------------------------- #
class DietlyHttpError(DietlyError):
    """An HTTP call to Dietly failed in a way we can describe.

    Attributes:
        status_code: HTTP status code, when one is known.
        url: Requested URL, when one is known.
        payload: Parsed response body, when it could be parsed.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.payload = payload


class ApiError(DietlyHttpError):
    """Dietly answered with an unexpected status code or an unexpected body."""


class NotFoundError(DietlyHttpError):
    """A requested object (order, delivery, slot) does not exist."""


class CateringMismatchError(DietlyHttpError):
    """HTTP 490 — the order belongs to a different catering (``company-id``)."""


class CloudflareBlockedError(DietlyHttpError):
    """Cloudflare challenged or blocked the request (browser check / 503)."""


class NetworkError(DietlyError):
    """The request never reached Dietly (DNS, TLS, timeout, proxy, ...)."""


class UnknownMealOptionError(DietlyError):
    """The requested replacement meal was not among the offered options."""


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
class MissingSessionError(DietlyError):
    """No stored session exists yet; the user has to log in first."""


class InvalidCredentialsError(DietlyHttpError):
    """The login endpoint rejected the supplied email/password."""


class SessionExpiredError(DietlyHttpError):
    """The stored session cookie is no longer accepted by Dietly."""
