"""Tests for the append-only write audit log (:mod:`dietly_mcp.audit`).

No network, no credentials: the log is a plain JSONL file in ``tmp_path``.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from dietly_mcp.audit import AUDIT_FIELDS, AuditLog, default_audit_log, new_audit_id
from dietly_mcp.config import Settings, load_settings


def _record(log: AuditLog, **overrides: object) -> dict:
    fields: dict[str, object] = {
        "event": "switch",
        "result": "ok",
        "dry_run": False,
        "tool": "t",
        "order_id": 1,
        "catering": "example-catering",
        "delivery_id": 2,
        "date": "2026-10-29",
        "slot": "Kolacja",
        "delivery_meal_id": 3,
        "from_": {"id": 1, "name": "A", "kcal": 100},
        "to": {"id": 2, "name": "B", "kcal": 200},
    }
    fields.update(overrides)
    return log.record(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_record_writes_one_jsonl_line_with_every_field(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    entry = _record(AuditLog(path))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])

    assert row == entry
    for field in AUDIT_FIELDS:
        assert field in row
    assert row["id"] == entry["id"]
    assert len(row["id"]) == 36  # a UUID4
    assert row["timestamp"].endswith("+00:00")  # ISO 8601, UTC
    assert row["from"] == {"id": 1, "name": "A", "kcal": 100}
    assert row["to"] == {"id": 2, "name": "B", "kcal": 200}


def test_log_is_append_only_and_never_rewrites(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    _record(log, event="dry_run", dry_run=True)
    _record(log, event="switch")
    _record(log, event="revert")

    assert [row["event"] for row in log.entries()] == ["dry_run", "switch", "revert"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_extra_fields_do_not_shadow_the_required_ones(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")

    entry = _record(log, extra={"revertedAuditId": "abc", "id": "hijacked"})

    assert entry["revertedAuditId"] == "abc"
    assert entry["id"] != "hijacked"  # the generated id wins


# --------------------------------------------------------------------------- #
# Permissions and robustness
# --------------------------------------------------------------------------- #
def test_audit_file_and_dir_are_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "audit.jsonl"
    _record(AuditLog(path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_readers_tolerate_missing_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    assert AuditLog(path).entries() == []

    path.write_text('{"id": "a"}\nnot json\n\n[1, 2]\n{"id": "b"}\n', encoding="utf-8")

    assert [row["id"] for row in AuditLog(path).entries()] == ["a", "b"]


def test_entries_limit_keeps_the_most_recent(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(5):
        _record(log, message=str(index))

    assert [row["message"] for row in log.entries(limit=2)] == ["3", "4"]
    assert len(log.entries()) == 5


# --------------------------------------------------------------------------- #
# Lookups used by revert
# --------------------------------------------------------------------------- #
def test_get_finds_an_entry_by_id(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = _record(log, message="first")
    second = _record(log, message="second")

    assert log.get(second["id"])["message"] == "second"
    assert log.get(first["id"])["message"] == "first"
    assert log.get("nope") is None
    assert new_audit_id() != first["id"]


def test_last_applied_skips_dry_runs_failures_and_reverts(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    _record(log, event="dry_run", dry_run=True)
    applied = _record(log, event="switch", date="2026-10-27", slot="Obiad")
    _record(log, event="revert", date="2026-10-27", slot="Obiad")
    _record(log, event="switch", result="error")
    other = _record(log, event="switch", date="2026-10-29", slot="Kolacja")

    assert log.last_applied()["id"] == other["id"]  # most recent applied switch
    assert log.last_applied(date="2026-10-27")["id"] == applied["id"]
    assert log.last_applied(slot="kolacja")["id"] == other["id"]
    assert log.last_applied(date="2026-10-27", slot="Kolacja") is None
    assert log.last_applied(date="2026-01-01") is None


# --------------------------------------------------------------------------- #
# Path resolution / configuration
# --------------------------------------------------------------------------- #
def test_default_audit_log_follows_settings(tmp_path: Path) -> None:
    settings = Settings(audit_path=tmp_path / "custom" / "audit.jsonl")

    assert default_audit_log(settings).path == tmp_path / "custom" / "audit.jsonl"


def test_audit_path_is_overridable_by_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DIETLY_AUDIT_FILE", str(tmp_path / "env" / "audit.jsonl"))

    settings = load_settings()

    assert settings.audit_path == tmp_path / "env" / "audit.jsonl"
    assert default_audit_log(settings).path == settings.audit_path


def test_audit_path_defaults_under_the_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DIETLY_AUDIT_FILE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/dietly-state")

    settings = load_settings()

    assert str(settings.audit_path) == "/tmp/dietly-state/dietly-mcp/audit.jsonl"
