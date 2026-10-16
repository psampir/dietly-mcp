"""HTTP plumbing: IPv4-pinned transport, explicit timeouts, small bounded retries.

Two environment quirks are handled here rather than in every caller:

* **IPv6 is broken on some hosts** (it fails instantly, or hangs until the read
  timeout). Binding the local address to ``0.0.0.0`` makes httpx open ``AF_INET``
  sockets only, which pins every connection to IPv4 without touching resolver
  settings.
* **A single transient failure should not abort a login.** Connect/read errors
  are retried a couple of times with a small exponential backoff. HTTP status
  errors (4xx/5xx) are *not* retried — a 403 or 401 is an answer, not a flake.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

__all__ = [
    "RETRYABLE_EXCEPTIONS",
    "build_timeout",
    "build_transport",
    "send_with_retries",
]

#: Transport-level failures worth retrying: the request either never left the
#: machine (connect phase) or the connection died mid-flight (read/write).
#: ``httpx.HTTPStatusError`` is deliberately absent — status codes are answers.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def build_transport(*, ipv4_only: bool = True) -> httpx.HTTPTransport:
    """Return a transport pinned to IPv4 when ``ipv4_only`` is set.

    Args:
        ipv4_only: bind the local address to ``0.0.0.0``, which forces
            ``AF_INET`` and therefore avoids broken IPv6 routes. ``False``
            leaves the OS default (dual-stack) in place.
    """
    return httpx.HTTPTransport(local_address="0.0.0.0" if ipv4_only else None)


def build_timeout(*, read: float, connect: float) -> httpx.Timeout:
    """Return an explicit timeout: ``connect`` for dialing, ``read`` for the body.

    A separate, usually shorter connect timeout means a dead route (IPv6) fails
    fast instead of waiting for the full body timeout, while a slow Dietly
    response still gets the full ``read`` budget.
    """
    return httpx.Timeout(read, connect=connect)


def send_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff: float = 0.4,
    sleeper: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Send one request through ``client``, retrying transient errors.

    Args:
        client: the ``httpx.Client`` (or any object with ``.request``) to use.
        method: HTTP method.
        url: absolute URL.
        retries: extra attempts after the first one (``0`` disables retrying).
        backoff: base delay in seconds; doubles after each failed attempt.
        sleeper: sleep function, injectable so tests stay instant.
        **kwargs: forwarded to ``client.request`` (headers, params, data, ...).

    Returns:
        The first response that did not raise a transport error.

    Raises:
        httpx.HTTPError: the last transport error, once attempts are exhausted.
    """
    attempts = max(0, int(retries)) + 1
    last: httpx.HTTPError | None = None
    for attempt in range(attempts):
        try:
            return client.request(method, url, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last = exc
            if attempt + 1 < attempts:
                sleeper(backoff * (2**attempt))
    assert last is not None  # pragma: no cover - loop always sets it or returns
    raise last
