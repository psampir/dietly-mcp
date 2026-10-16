"""Append-only JSONL audit log for every Dietly write attempt.

One record is appended per *attempted* operation — including dry runs (marked
``dry_run: true``) and failures (``result: "error"``). The log is the only place
that remembers what a write was about to change, so it is what makes
:func:`dietly_mcp.operations.revert_operation` (and ``dietly-mcp revert``)
possible: a record keeps the *previous* selection (``from``) next to the new one
(``to``).

The file lives outside the repository, next to the session cookie jar
(``~/.local/state/dietly-mcp/audit.jsonl`` by default, directory ``700`` and file
``600``). The path is overridable with ``DIETLY_AUDIT_FILE``. It is opened in
append mode only — nothing in this project rewrites or truncates it.

Secret hygiene: only the whitelisted fields below are ever written. Credentials,
cookies and session/CSRF tokens have no field here and must never be added.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import default_audit_path, ensure_private_dir, ensure_private_file
from .errors import ConfigError

__all__ = ["AUDIT_EVENTS", "AUDIT_FIELDS", "AuditLog", "default_audit_log", "new_audit_id"]

#: The values ``event`` may take.
AUDIT_EVENTS = ("switch", "revert", "dry_run")

#: Fields every record carries. ``revertedAuditId`` is an optional extra.
AUDIT_FIELDS = (
    "id",
    "timestamp",
    "event",
    "dry_run",
    "result",
    "message",
    "order_id",
    "catering",
    "delivery_id",
    "date",
    "slot",
    "delivery_meal_id",
    "from",
    "to",
    "tool",
)


def new_audit_id() -> str:
    """Return a fresh audit id (a UUID4 string)."""
    return str(uuid.uuid4())


def _timestamp() -> str:
    """Return the current UTC instant as an ISO 8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuditLog:
    """Append-only JSONL writer/reader for one audit file.

    Args:
        path: the JSONL file. Parent directories are created on first write.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def record(
        self,
        *,
        event: str,
        result: str,
        message: str | None = None,
        dry_run: bool = False,
        tool: str | None = None,
        order_id: int | None = None,
        catering: str | None = None,
        delivery_id: int | None = None,
        date: str | None = None,
        slot: str | None = None,
        delivery_meal_id: int | None = None,
        from_: dict[str, Any] | None = None,
        to: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one record and return it.

        Args:
            event: ``switch``, ``revert`` or ``dry_run``.
            result: ``ok`` or ``error``.
            message: human-readable summary (never contains secrets).
            dry_run: whether the operation was a dry run (nothing was sent).
            tool: which tool/command produced the record.
            order_id / catering / delivery_id / date / slot / delivery_meal_id:
                the context of the operation.
            from_ / to: the previous and the new selection, each
                ``{"id": int, "name": str, "kcal": number}``.
            extra: additional (non-secret) fields, e.g. ``revertedAuditId``.

        Raises:
            ConfigError: the audit file could not be written.
        """
        entry: dict[str, Any] = {
            "id": new_audit_id(),
            "timestamp": _timestamp(),
            "event": event,
            "dry_run": bool(dry_run),
            "result": result,
            "message": message,
            "order_id": order_id,
            "catering": catering,
            "delivery_id": delivery_id,
            "date": date,
            "slot": slot,
            "delivery_meal_id": delivery_meal_id,
            "from": from_,
            "to": to,
            "tool": tool,
        }
        if extra:
            entry.update({key: value for key, value in extra.items() if key not in entry})
        self._append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        """Append one JSON line, creating the parent directory as needed."""
        try:
            ensure_private_dir(self.path.parent)
            line = json.dumps(entry, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            ensure_private_file(self.path)
        except OSError as exc:
            raise ConfigError(f"Could not append to the audit log {self.path}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def entries(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the log records in file order (oldest first).

        Unreadable or malformed lines are skipped rather than raising, so a
        truncated line can never make ``revert``/``audit`` unusable.

        Args:
            limit: keep only the ``limit`` most recent records.
        """
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        if limit is not None and limit >= 0:
            return rows[-limit:] if limit else []
        return rows

    def get(self, audit_id: str) -> dict[str, Any] | None:
        """Return the record with ``audit_id``, or ``None``."""
        for row in reversed(self.entries()):
            if row.get("id") == audit_id:
                return row
        return None

    def last_applied(
        self, *, date: str | None = None, slot: str | None = None
    ) -> dict[str, Any] | None:
        """Return the most recent applied (non-dry-run) ``switch`` record.

        Args:
            date: only consider records whose ``date`` matches (``YYYY-MM-DD``).
            slot: only consider records whose ``slot`` matches (case-insensitive).
        """
        for row in reversed(self.entries()):
            if row.get("event") != "switch" or row.get("result") != "ok":
                continue
            if row.get("dry_run"):
                continue
            if date and str(row.get("date") or "")[:10] != date:
                continue
            if slot and str(row.get("slot") or "").casefold() != slot.strip().casefold():
                continue
            return row
        return None


def default_audit_log(settings: Any | None = None) -> AuditLog:
    """Return the audit log for ``settings`` (falls back to the XDG default)."""
    path = getattr(settings, "audit_path", None) or default_audit_path()
    return AuditLog(path)
