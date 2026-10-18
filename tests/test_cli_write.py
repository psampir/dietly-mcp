"""CLI tests for the write commands (``switch``, ``revert``, ``audit``).

The CLI calls the same :mod:`dietly_mcp.operations` functions as the MCP write
tools, so these tests mostly pin the CLI surface: the exit codes, the printed
text, the ``--json`` shapes and the audit records. Everything is mocked; no test
touches the network.
"""

from __future__ import annotations

import json as jsonlib
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_cli_polish import CURRENT_ROW, DELIVERY_ID, ORDER_ID, SLOT_ID, CliFake

from dietly_mcp import cli
from dietly_mcp.audit import AuditLog
from dietly_mcp.config import Settings

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=10)).isoformat()
NEAR = TODAY.isoformat()


def _delivery(delivery_id: int = DELIVERY_ID, day: str = FUTURE) -> dict:
    return {"deliveryId": delivery_id, "date": day, "deliveryMeals": []}


def _seed_applied_switch(log: AuditLog, **overrides: object) -> dict:
    """Write the kind of record ``switch --apply`` would have written."""
    fields: dict[str, object] = {
        "event": "switch",
        "result": "ok",
        "dry_run": False,
        "tool": "switch",
        "order_id": ORDER_ID,
        "catering": "example-catering",
        "delivery_id": DELIVERY_ID,
        "date": FUTURE,
        "slot": "Obiad",
        "delivery_meal_id": SLOT_ID,
        "from_": {"id": 2400, "name": "Kurczak pieczony z kaszą", "kcal": 700},
        "to": {"id": 2401, "name": "Łosoś z ryżem", "kcal": 520},
    }
    fields.update(overrides)
    return log.record(**fields)  # type: ignore[arg-type]


@pytest.fixture
def cli_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install a ``CliFake`` + a throwaway audit log into the CLI."""
    audit_path = tmp_path / "state" / "audit.jsonl"
    settings = Settings(
        session_path=tmp_path / "state" / "session.json",
        audit_path=audit_path,
    )

    def _install(fake: CliFake) -> tuple[CliFake, AuditLog]:
        monkeypatch.setattr(cli, "_client", lambda: (fake, settings))
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        return fake, AuditLog(audit_path)

    return _install


# --------------------------------------------------------------------------- #
# switch: audit + --json shape
# --------------------------------------------------------------------------- #
def test_switch_dry_run_records_an_audit_entry_and_sends_nothing(cli_write) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    assert cli.run_cli(["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "2401"]) == 0

    assert [call["dry_run"] for call in fake.switch_calls] == [True]
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["event"] == "dry_run"
    assert entries[0]["result"] == "ok"
    assert entries[0]["from"]["id"] == 2400
    assert entries[0]["to"]["id"] == 2401


def test_switch_apply_json_carries_the_audit_id(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    assert (
        cli.run_cli(
            ["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "2401", "--apply", "--json"]
        )
        == 0
    )
    payload = jsonlib.loads(capsys.readouterr().out)

    assert payload["applied"] is True
    assert payload["date"] == FUTURE
    assert payload["dietCaloriesMealId"] == 2401  # unchanged, API-shaped keys
    assert payload["auditId"] == log.entries()[-1]["id"]
    assert fake.switch_calls[0]["dry_run"] is False


def test_switch_apply_human_output_shows_macros_for_both_dishes(cli_write, capsys) -> None:
    fake, _ = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    assert (
        cli.run_cli(["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "2401", "--apply"])
        == 0
    )
    out = capsys.readouterr().out

    # The post-apply result shows macros for both the previous and the new dish.
    assert "544 kcal | B 38.1g / W 33.4g / T 15.5g" in out
    assert "519 kcal | B 42g / W 43.5g / T 18.8g" in out
    assert fake.switch_calls[0]["dry_run"] is False


def test_switch_refuses_an_unoffered_target_with_a_clean_error(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    exit_code = cli.run_cli(
        ["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "9999", "--apply"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "9999" in captured.err
    assert "Traceback" not in captured.err
    assert fake.switch_calls == []
    assert log.entries()[-1]["result"] == "error"


# --------------------------------------------------------------------------- #
# revert
# --------------------------------------------------------------------------- #
def test_revert_last_switch_restores_the_recorded_meal(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    original = _seed_applied_switch(log)

    assert cli.run_cli(["revert"]) == 0

    output = capsys.readouterr().out
    assert f"Reverted Obiad on {FUTURE}" in output
    assert original["id"] in output
    # One real write, back to the meal the audit recorded as "from".
    assert fake.switch_calls == [{"target_id": 2400, "dry_run": False, "amount": 1}]

    entries = log.entries()
    assert [e["event"] for e in entries] == ["switch", "revert"]
    assert entries[-1]["revertedAuditId"] == original["id"]
    assert entries[-1]["from"]["id"] == 2400
    assert entries[-1]["to"]["id"] == 2400


def test_revert_by_audit_id_is_precise(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    first = _seed_applied_switch(log, date=FUTURE, slot="Obiad")
    second = _seed_applied_switch(log, date=FUTURE, slot="Śniadanie", from_={"id": 2402})

    assert cli.run_cli(["revert", "--audit-id", first["id"]]) == 0

    assert first["id"] in capsys.readouterr().out
    assert fake.switch_calls[0]["target_id"] == 2400
    assert second["id"] not in {e.get("revertedAuditId") for e in log.entries()}


def test_revert_dry_run_sends_nothing(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    _seed_applied_switch(log)

    assert cli.run_cli(["revert", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Would revert" in output
    assert fake.switch_calls[0]["dry_run"] is True
    assert log.entries()[-1]["event"] == "dry_run"


def test_revert_without_any_record_is_a_clean_error(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    exit_code = cli.run_cli(["revert"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "audit" in captured.err.lower()
    assert "Traceback" not in captured.err
    assert fake.switch_calls == []
    assert log.entries()[-1]["result"] == "error"


def test_revert_refuses_when_the_recorded_meal_is_not_offered(cli_write, capsys) -> None:
    fake, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    _seed_applied_switch(log, from_={"id": 9999, "name": "Wycofane", "kcal": 1})

    exit_code = cli.run_cli(["revert"])

    assert exit_code == 1
    assert "9999" in capsys.readouterr().err
    assert fake.switch_calls == []


def test_revert_refuses_when_the_day_is_locked(cli_write, capsys) -> None:
    fake, log = cli_write(
        CliFake(orders=[CURRENT_ROW], deliveries=[_delivery(day=NEAR)], editable=None)
    )
    _seed_applied_switch(log, date=NEAR)

    exit_code = cli.run_cli(["revert"])

    assert exit_code == 1
    assert "cutoff" in capsys.readouterr().err
    assert fake.switch_calls == []


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
def test_audit_renders_a_human_table(cli_write, capsys) -> None:
    _, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    _seed_applied_switch(log, slot="Kolacja")

    assert cli.run_cli(["audit"]) == 0

    output = capsys.readouterr().out
    assert "EVENT" in output
    assert "RESULT" in output
    assert "switch" in output
    assert "Kolacja" in output
    assert "2400 Kurczak pieczony z kaszą → 2401 Łosoś z ryżem" in output


def test_audit_json_is_machine_readable(cli_write, capsys) -> None:
    _, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    entry = _seed_applied_switch(log)

    assert cli.run_cli(["audit", "--json"]) == 0

    payload = jsonlib.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[-1]["id"] == entry["id"]
    assert payload[-1]["event"] == "switch"
    assert payload[-1]["from"]["id"] == 2400
    assert payload[-1]["to"]["id"] == 2401


def test_audit_limit_keeps_the_most_recent(cli_write, capsys) -> None:
    _, log = cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))
    _seed_applied_switch(log, message="first")
    _seed_applied_switch(log, message="second")

    assert cli.run_cli(["audit", "--limit", "1", "--json"]) == 0

    payload = jsonlib.loads(capsys.readouterr().out)
    assert [row["message"] for row in payload] == ["second"]


def test_audit_says_so_when_there_is_nothing_yet(cli_write, capsys) -> None:
    cli_write(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery()]))

    assert cli.run_cli(["audit"]) == 0

    assert "No audit entries yet" in capsys.readouterr().out


def test_help_lists_the_new_commands(capsys) -> None:
    assert cli.run_cli(["--help"]) == 0

    output = capsys.readouterr().out
    for command in ("switch", "revert", "audit"):
        assert command in output
