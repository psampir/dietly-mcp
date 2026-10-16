"""Paths, settings and credential loading.

Secrets live *outside* the repository:

* credentials: ``~/.config/dietly-mcp/credentials.env`` (``KEY=VALUE`` lines,
  parsed — never shell-sourced) with ``DIETLY_*`` environment variables as a
  fallback;
* session cookie jar: ``~/.local/state/dietly-mcp/session.json``;
* append-only write audit log: ``~/.local/state/dietly-mcp/audit.jsonl``.

Both roots are **overridable for containers**: ``DIETLY_CONFIG_DIR`` replaces the
whole config directory (so ``DIETLY_CONFIG_DIR=/config`` puts the credentials at
``/config/credentials.env``) and ``DIETLY_STATE_DIR`` replaces the whole state
directory (so ``DIETLY_STATE_DIR=/data`` puts the cookie jar at
``/data/session.json`` and the audit log at ``/data/audit.jsonl``). The narrower
``DIETLY_CREDENTIALS_FILE`` / ``DIETLY_SESSION_FILE`` / ``DIETLY_AUDIT_FILE``
variables override one path each and win over the directory variables.

This module never prints secret values. It only warns about weak file
permissions; the ``*_configured`` helpers return booleans so callers (the
``/healthz`` endpoint) can report readiness without exposing anything.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DietlyError, MissingCredentialsError
from .logic import DEFAULT_EDIT_CUTOFF_HOURS

APP_NAME = "dietly-mcp"
ENV_PREFIX = "DIETLY_"

DEFAULT_BASE_URL = "https://panel.dietly.pl"
DEFAULT_LOGIN_URL = "https://panel.dietly.pl/api/auth/login"
DEFAULT_COMPANY_ID = "wybormenu"
DEFAULT_ACCEPT_LANGUAGE = "pl-PL,pl;q=0.9,en;q=0.8"
# Believable recent Firefox on Linux — the site sits behind Cloudflare.
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0"
DEFAULT_TIMEOUT = 30.0
#: Bound on dialing: a dead (IPv6) route should fail fast, not wait for the body.
DEFAULT_CONNECT_TIMEOUT = 10.0
#: Extra attempts after a transient connect/read error.
DEFAULT_RETRIES = 2

CREDENTIALS_FILENAME = "credentials.env"
AUDIT_FILENAME = "audit.jsonl"

#: Paths already warned about, so the warning is printed only once per process.
_WARNED_PERMISSIONS: set[Path] = set()


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    """Return the user config directory.

    ``DIETLY_CONFIG_DIR`` wins outright (containers mount ``/config``); otherwise
    the XDG default (``$XDG_CONFIG_HOME`` aware) plus ``/dietly-mcp``.
    """
    override = os.environ.get(ENV_PREFIX + "CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / APP_NAME


def state_dir() -> Path:
    """Return the user state directory.

    ``DIETLY_STATE_DIR`` wins outright (containers mount ``/data``); otherwise
    the XDG default (``$XDG_STATE_HOME`` aware) plus ``/dietly-mcp``.
    """
    override = os.environ.get(ENV_PREFIX + "STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / APP_NAME


def default_credentials_path() -> Path:
    """Return the default credentials file path."""
    return config_dir() / CREDENTIALS_FILENAME


def default_session_path() -> Path:
    """Return the default session cookie jar path."""
    return state_dir() / "session.json"


def default_audit_path() -> Path:
    """Return the default write-audit JSONL path."""
    return state_dir() / AUDIT_FILENAME


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (parents included) with mode ``700``."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        path.chmod(0o700)
    return path


def ensure_private_file(path: Path) -> Path:
    """Restrict ``path`` to mode ``600``."""
    with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
        path.chmod(0o600)
    return path


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Credentials:
    """An email/password pair plus the place it came from.

    ``source`` is safe to print (it is a path or ``"environment"``), ``username``
    and ``password`` are not.
    """

    username: str
    password: str
    source: str = "unknown"

    def email(self) -> str:
        """Return the login email (alias for :attr:`username`)."""
        return self.username


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines without invoking a shell.

    Blank lines and ``#`` comments are ignored. Surrounding single or double
    quotes around a value are stripped. ``export `` prefixes are tolerated for
    convenience.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _pick(values: dict[str, str], *names: str) -> str | None:
    """Return the first non-empty value for any of ``names`` (case-insensitive)."""
    lowered = {key.upper(): value for key, value in values.items()}
    for name in names:
        value = lowered.get(name.upper())
        if value:
            return value
    return None


def insecure_permissions_warning(path: Path) -> str | None:
    """Return a warning if ``path`` is group/world accessible, else ``None``."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (
            "======================================================================\n"
            f" WARNING: {path} is readable by other users (mode {mode:03o}).\n"
            " Your Dietly password may leak.\n"
            f" Fix it with:  chmod 600 {path}\n"
            "======================================================================"
        )
    return None


def warn_if_insecure(path: Path, *, stream: object | None = None) -> None:
    """Print a big, visible warning to stderr when ``path`` is too open."""
    path = Path(path)
    if path in _WARNED_PERMISSIONS:
        return
    warning = insecure_permissions_warning(path)
    if warning:
        _WARNED_PERMISSIONS.add(path)
        print(warning, file=stream or sys.stderr, flush=True)


def load_credentials(path: Path | str | None = None, *, env: bool = True) -> Credentials:
    """Load credentials from the credentials file, falling back to the environment.

    Accepted keys (both bare and ``DIETLY_``-prefixed):
    ``DIETLY_EMAIL`` / ``DIETLY_USERNAME`` / ``EMAIL`` / ``USERNAME`` and
    ``DIETLY_PASSWORD`` / ``PASSWORD``.

    Raises:
        MissingCredentialsError: nothing usable was found.
    """
    file_path = Path(path) if path is not None else default_credentials_path()
    username: str | None = None
    password: str | None = None
    source = "environment"
    missing_hint = ""

    if file_path.exists():
        warn_if_insecure(file_path)
        values = parse_env_file(file_path.read_text(encoding="utf-8"))
        username = _pick(values, "DIETLY_EMAIL", "DIETLY_USERNAME", "EMAIL", "USERNAME")
        password = _pick(values, "DIETLY_PASSWORD", "PASSWORD")
        source = str(file_path)

    if env:
        if username is None:
            username = os.environ.get(ENV_PREFIX + "EMAIL") or os.environ.get(
                ENV_PREFIX + "USERNAME"
            )
            if username:
                source = "environment"
        if password is None:
            password = os.environ.get(ENV_PREFIX + "PASSWORD")
            if password:
                source = "environment"

    if username and password:
        return Credentials(username=username, password=password, source=source)

    if not file_path.exists():
        missing_hint = (
            f"No credentials file at {file_path}.\n"
            "Create it with:\n"
            f"  mkdir -p {file_path.parent}\n"
            f"  printf '%s\\n' 'DIETLY_EMAIL=you@example.com' 'DIETLY_PASSWORD=<your-password>'"
            f" > {file_path}\n"
            f"  chmod 600 {file_path}\n"
            "Alternatively export DIETLY_EMAIL and DIETLY_PASSWORD."
        )
    else:
        missing_hint = (
            f"{file_path} exists but does not contain both an email and a password.\n"
            "Expected lines:\n"
            "  DIETLY_EMAIL=you@example.com\n"
            "  DIETLY_PASSWORD=secret"
        )
    raise MissingCredentialsError(missing_hint)


def credentials_configured(path: Path | str | None = None, *, env: bool = True) -> bool:
    """Return whether an email/password pair is available — as a **boolean only**.

    Never returns (or logs) the values; used by ``GET /healthz`` so a container
    orchestrator can see readiness without any account data leaving the process.
    """
    try:
        load_credentials(path, env=env)
    except (DietlyError, OSError):
        return False
    return True


def session_configured(path: Path | str | None = None) -> bool:
    """Return whether a stored session cookie jar exists (boolean only)."""
    file_path = Path(path) if path is not None else default_session_path()
    return file_path.is_file()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """Runtime settings for the client; all fields are overridable by env vars."""

    base_url: str = DEFAULT_BASE_URL
    login_url: str = DEFAULT_LOGIN_URL
    company_id: str = DEFAULT_COMPANY_ID
    user_agent: str = DEFAULT_USER_AGENT
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE
    timeout: float = DEFAULT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    ipv4_only: bool = True
    #: Deliveries closer than this are assumed locked; used by the shared
    #: editable heuristic (``DIETLY_EDIT_CUTOFF_HOURS``).
    edit_cutoff_hours: float = DEFAULT_EDIT_CUTOFF_HOURS
    credentials_path: Path = field(default_factory=default_credentials_path)
    session_path: Path = field(default_factory=default_session_path)
    #: Append-only audit log for writes (``DIETLY_AUDIT_FILE``).
    audit_path: Path = field(default_factory=default_audit_path)


def _as_bool(raw: str | None, default: bool) -> bool:
    """Parse a truthy/falsy environment string (``0``/``false``/``no`` are false)."""
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _as_float(raw: str | None, default: float) -> float:
    """Parse a float environment string, falling back to ``default``."""
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _as_int(raw: str | None, default: int) -> int:
    """Parse an int environment string, falling back to ``default``."""
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_settings() -> Settings:
    """Build :class:`Settings` from ``DIETLY_*`` environment variables."""
    env = os.environ

    return Settings(
        base_url=env.get(ENV_PREFIX + "BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        login_url=env.get(ENV_PREFIX + "LOGIN_URL", DEFAULT_LOGIN_URL),
        company_id=env.get(ENV_PREFIX + "COMPANY_ID", DEFAULT_COMPANY_ID),
        user_agent=env.get(ENV_PREFIX + "USER_AGENT", DEFAULT_USER_AGENT),
        accept_language=env.get(ENV_PREFIX + "ACCEPT_LANGUAGE", DEFAULT_ACCEPT_LANGUAGE),
        timeout=_as_float(env.get(ENV_PREFIX + "TIMEOUT"), DEFAULT_TIMEOUT),
        connect_timeout=_as_float(
            env.get(ENV_PREFIX + "CONNECT_TIMEOUT"), DEFAULT_CONNECT_TIMEOUT
        ),
        retries=max(0, _as_int(env.get(ENV_PREFIX + "RETRIES"), DEFAULT_RETRIES)),
        ipv4_only=_as_bool(env.get(ENV_PREFIX + "IPV4_ONLY"), True),
        edit_cutoff_hours=_as_float(
            env.get(ENV_PREFIX + "EDIT_CUTOFF_HOURS"), DEFAULT_EDIT_CUTOFF_HOURS
        ),
        credentials_path=Path(
            env.get(ENV_PREFIX + "CREDENTIALS_FILE", str(default_credentials_path()))
        ).expanduser(),
        session_path=Path(
            env.get(ENV_PREFIX + "SESSION_FILE", str(default_session_path()))
        ).expanduser(),
        audit_path=Path(
            env.get(ENV_PREFIX + "AUDIT_FILE", str(default_audit_path()))
        ).expanduser(),
    )
