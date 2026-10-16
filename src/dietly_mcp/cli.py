"""Typer command line app for dietly-mcp.

Commands print English text; read commands accept ``--json`` for
machine-readable output. Expected errors (bad credentials, expired session,
HTTP 490, Cloudflare, network) print a single clean message instead of a
traceback.

The write commands are ``switch`` (a dry run unless ``--apply`` is given) and
``revert`` (applies by default, ``--dry-run`` to preview); ``audit`` lists the
append-only log they maintain. ``switch`` and ``revert`` call the same
:mod:`dietly_mcp.operations` functions as the MCP write tools, so the guardrail,
the dry-run semantics and the audit records cannot drift between front ends.
"""

from __future__ import annotations

import json as jsonlib
import sys
from collections.abc import Iterable, Sequence
from typing import Any

import typer

from . import __version__
from .audit import default_audit_log
from .client import DietlyClient, resolve_delivery, resolve_slot
from .config import Settings, load_credentials, load_settings
from .errors import DietlyError
from .logic import (
    allergen_labels,
    current_orders,
    is_editable,
    nutrition_kcal,
    parse_date,
    sort_deliveries,
)
from .models import DayMeal, Delivery, Order
from .operations import revert_operation, switch_operation
from .session import Session

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Unofficial Dietly (dietly.pl) client — inspect orders, daily menus and switch meals.",
)

JSON_OPTION = typer.Option(False, "--json", help="Machine-readable JSON output.")

ORDER_OPTION = typer.Option(
    None,
    "--order",
    "-o",
    help="Order id; required only when several orders are active.",
)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def _err(message: str) -> None:
    """Print an error message to stderr."""
    print(message, file=sys.stderr)


def _dump_json(payload: Any) -> None:
    """Print ``payload`` as indented UTF-8 JSON."""
    typer.echo(jsonlib.dumps(payload, ensure_ascii=False, indent=2))


def _dump_model(model: Any) -> dict[str, Any]:
    """Serialise a model (or plain value) to an API-shaped dict."""
    if hasattr(model, "to_dict"):
        return model.to_dict(camel=True, exclude_none=True)
    return {"value": model}


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Render a simple fixed-width text table (no extra dependencies)."""
    string_rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in string_rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in string_rows:
        lines.append(
            "  ".join(row[i].ljust(widths[i]) for i in range(min(len(widths), len(row)))).rstrip()
        )
    if not string_rows:
        lines.append("(none)")
    return "\n".join(lines)


def _yes_no(value: bool | None) -> str:
    """Render a tri-state boolean."""
    if value is None:
        return "-"
    return "yes" if value else "no"


def _kcal(model: Any) -> str:
    """Extract a kcal figure from a ``nutrition`` dict, when present."""
    value = nutrition_kcal(model)
    if value is None:
        return "-"
    return f"{value:g}"


def _ids(model: Any) -> str:
    """Compact id pair for meals/options."""
    diet_id = getattr(model, "diet_calories_meal_id", None)
    slot_id = getattr(model, "delivery_meal_id", None)
    parts = [f"meal={diet_id}"]
    if slot_id:
        parts.append(f"slot={slot_id}")
    return " ".join(parts)


def _order_line(order: Order) -> str:
    """One-line order header."""
    span = ""
    if order.date_from and order.date_to:
        span = f"  {order.date_from} → {order.date_to}"
    return f"Order {order.order_id} ({order.company_name or '?'}){span}"


# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #
def _load_session(settings: Settings) -> Session:
    """Load the persisted session or explain how to create one."""
    return Session.load(settings)


def _client() -> tuple[DietlyClient, Settings]:
    """Return a client built on the stored session, plus the settings."""
    settings = load_settings()
    session = _load_session(settings)
    return DietlyClient(session), settings


def _select_orders(client: DietlyClient, order_id: int | None) -> list[Order]:
    """Resolve ``--order``; auto-select the single active order when unambiguous."""
    active = client.get_orders(active_only=True)
    if order_id is not None:
        for order in active:
            if order.order_id == order_id:
                return [order]
        known = client.get_orders()
        if any(order.order_id == order_id for order in known):
            raise DietlyError(f"Order {order_id} exists but is not active.")
        ids = ", ".join(str(o.order_id) for o in known if o.order_id is not None) or "none"
        raise DietlyError(f"No order with id {order_id}. Known order ids: {ids}.")
    if len(active) == 1:
        return active
    if not active:
        raise DietlyError("No active orders found for this account.")
    listing = "\n".join(f"  - {order.label}" for order in active)
    raise DietlyError(f"Several active orders found — re-run with --order <id>:\n{listing}")


def _parse_date(value: str) -> str:
    """Validate a ``YYYY-MM-DD`` date (``today``/``tomorrow`` also accepted)."""
    try:
        return parse_date(value)
    except ValueError as exc:
        raise DietlyError(
            f"Invalid --date {value!r}: expected YYYY-MM-DD (or 'today'/'tomorrow')."
        ) from exc


def _resolve_day(client: DietlyClient, order: Order, day: str) -> tuple[Delivery, list[DayMeal]]:
    """Resolve ISO date ``day`` to a delivery and its current menu."""
    detail = client.get_order(order.order_id)  # type: ignore[arg-type]
    delivery = resolve_delivery(detail, day)
    if delivery.delivery_id is None:
        raise DietlyError(f"Delivery on {day} has no deliveryId in the API response.")
    meals = client.get_day_menu(order.order_id, delivery.delivery_id)  # type: ignore[arg-type]
    return delivery, meals


def _kcal_text(value: Any) -> str:
    """Format a kcal figure for the human-readable plan (``544.0`` → ``544``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-" if value is None else str(value)
    return f"{value:g}"


def _print_switch_plan(payload: dict[str, Any]) -> None:
    """Explain a dry-run switch from the operations payload."""
    from_ref = payload.get("from") or {}
    to_ref = payload.get("to") or {}
    variant = f", option {to_ref['variant']}" if to_ref.get("variant") else ""
    lines = [
        "DRY RUN — nothing was sent to Dietly.",
        f"  Order:   {payload.get('orderId')}",
        f"  Date:    {payload.get('date')}",
        f"  Slot:    {payload.get('slot')} (slot id {payload.get('deliveryMealId')})",
        f"  From:    {from_ref.get('name') or '?'} "
        f"(meal id {from_ref.get('id')}, {_kcal_text(from_ref.get('kcal'))} kcal)",
        f"  To:      {to_ref.get('name') or '?'} "
        f"(meal id {to_ref.get('id')}, {_kcal_text(to_ref.get('kcal'))} kcal{variant})",
        f"  Request: PUT {(payload.get('request') or {}).get('url')}",
        "",
        "Re-run with --apply to perform this switch.",
    ]
    typer.echo("\n".join(lines))


def _switch_plan_json(payload: dict[str, Any]) -> dict[str, Any]:
    """The machine-readable dry-run plan (kept stable for existing consumers)."""
    from_ref = payload.get("from") or {}
    to_ref = payload.get("to") or {}
    return {
        "dryRun": True,
        "applied": False,
        "auditId": payload.get("auditId"),
        "orderId": payload.get("orderId"),
        "date": payload.get("date"),
        "slot": payload.get("slot"),
        "deliveryId": payload.get("deliveryId"),
        "deliveryMealId": payload.get("deliveryMealId"),
        "from": {
            "dietCaloriesMealId": from_ref.get("id"),
            "menuMealName": from_ref.get("name"),
            "kcal": from_ref.get("kcal"),
        },
        "to": {
            "dietCaloriesMealId": to_ref.get("id"),
            "menuMealName": to_ref.get("name"),
            "dietOptionName": to_ref.get("variant"),
            "kcal": to_ref.get("kcal"),
        },
        "request": payload.get("request"),
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command()
def login() -> None:
    """Log in to Dietly and persist the session cookie.

    Does the full browser handshake: fetch the CSRF cookie (GET), then POST the
    credentials with the ``X-XSRF-TOKEN`` header.
    """
    settings = load_settings()
    credentials = load_credentials(settings.credentials_path)
    session = Session.login(credentials, settings)

    email = credentials.username
    try:
        session.verify()
        email = session.email or email
        session.save()
    except DietlyError as exc:
        _err(f"Warning: logged in, but session verification failed: {exc}")

    typer.echo(f"OK, logged in as {email}")
    typer.echo(f"Session stored in {settings.session_path} (mode 600)")


@app.command()
def status(json_output: bool = JSON_OPTION) -> None:
    """Show session validity and an overview of active orders."""
    client, settings = _client()
    session = client.session
    try:
        session.verify()
        valid = True
        email = session.email
    except DietlyError:
        valid = False
        email = session.email

    active = client.get_orders(active_only=True) if valid else []
    if json_output:
        _dump_json(
            {
                "session": {
                    "valid": valid,
                    "email": email,
                    "file": str(settings.session_path),
                },
                "activeOrders": [_dump_model(order) for order in active],
            }
        )
        return

    typer.echo(
        f"Session: {'valid' if valid else 'EXPIRED'} ({email or 'unknown'})"
        f"  [{settings.session_path}]"
    )
    if not valid:
        raise DietlyError("Stored session is no longer valid. Run `dietly-mcp login`.")
    typer.echo("")
    _print_orders(active)


def _print_orders(orders: Sequence[Order]) -> None:
    """Render an orders table."""
    if not orders:
        typer.echo("No active orders.")
        return
    rows = [
        [
            order.order_id,
            order.status,
            order.company_name,
            order.diet_name,
            order.diet_calories,
            order.date_from,
            order.date_to,
        ]
        for order in orders
    ]
    typer.echo(_table(["ID", "STATUS", "CATERING", "DIET", "KCAL", "FROM", "TO"], rows))


@app.command()
def orders(
    json_output: bool = JSON_OPTION,
    all_orders: bool = typer.Option(
        False,
        "--all",
        help="Include every order, including historical ones (skips the date filter).",
    ),
) -> None:
    """List orders that are still running (use --all to include history)."""
    client, _ = _client()
    known = client.get_orders(active_only=False)
    selected = known if all_orders else current_orders(known)
    if json_output:
        _dump_json([_dump_model(order) for order in selected])
        return
    _print_orders(selected)
    if not all_orders and len(selected) < len(known):
        typer.echo(
            f"\n({len(known) - len(selected)} historical order(s) hidden; "
            "run with --all to list them.)"
        )


@app.command()
def deliveries(
    order: int = ORDER_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List delivery days per active order, with an *estimated* editable flag."""
    client, settings = _client()
    selected = _select_orders(client, order)

    payload: list[dict[str, Any]] = []
    text_blocks: list[str] = []
    heuristic_seen = False
    for current in selected:
        detail = client.get_order(current.order_id)  # type: ignore[arg-type]
        rows = []
        for delivery in sort_deliveries(detail.deliveries):
            editable = delivery.editable
            if editable is None:
                editable = is_editable(
                    delivery.date, cutoff_hours=settings.edit_cutoff_hours
                )
                heuristic_seen = True
            rows.append(
                [
                    delivery.date,
                    delivery.delivery_id,
                    _yes_no(editable),
                    len(delivery.delivery_meals),
                ]
            )
            payload.append(
                {
                    "orderId": current.order_id,
                    "deliveryId": delivery.delivery_id,
                    "date": delivery.date,
                    "editable": editable,
                    "editableIsHeuristic": delivery.editable is None,
                    "mealCount": len(delivery.delivery_meals),
                }
            )
        text_blocks.append(_order_line(current))
        text_blocks.append(_table(["DATE", "DELIVERY ID", "EDITABLE", "MEALS"], rows))

    if json_output:
        _dump_json(payload)
        return

    typer.echo("\n".join(text_blocks))
    if heuristic_seen:
        typer.echo(
            f"\nNote: this API version does not expose an editable flag, so EDITABLE is "
            f"estimated: a day is assumed editable when it starts at least "
            f"{settings.edit_cutoff_hours:g}h from now (DIETLY_EDIT_CUTOFF_HOURS). "
            "It is a heuristic — Dietly can still reject a change."
        )


@app.command()
def day(
    date: str = typer.Option(..., "--date", help="Delivery date, YYYY-MM-DD (or today/tomorrow)."),
    order: int = ORDER_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """Show the meals currently chosen for every slot of one day."""
    client, _ = _client()
    current = _select_orders(client, order)[0]
    iso_date = _parse_date(date)
    _, meals = _resolve_day(client, current, iso_date)

    if json_output:
        _dump_json(
            {
                "orderId": current.order_id,
                "date": iso_date,
                "meals": [_dump_model(meal) for meal in meals],
            }
        )
        return

    typer.echo(f"{_order_line(current)} — {iso_date}")
    rows = [
        [
            meal.meal_name,
            meal.menu_meal_name,
            meal.diet_calories_meal_id,
            meal.delivery_meal_id,
            _yes_no(meal.switchable),
            _allergens_text(meal.allergens),
        ]
        for meal in meals
    ]
    typer.echo(_table(["SLOT", "DISH", "MEAL ID", "SLOT ID", "SWITCHABLE", "ALLERGENS"], rows))


def _allergens_text(allergens: Sequence[Any]) -> str:
    """Render allergen entries (raw API values) as a comma-separated string."""
    return ", ".join(allergen_labels(allergens)) or "-"


@app.command()
def options(
    date: str = typer.Option(..., "--date", help="Delivery date, YYYY-MM-DD (or today/tomorrow)."),
    slot: str = typer.Option(..., "--slot", help='Slot name as used by Dietly, e.g. "Obiad".'),
    order: int = ORDER_OPTION,
    json_output: bool = JSON_OPTION,
) -> None:
    """List the alternative meals offered for one slot of one day."""
    client, _ = _client()
    current = _select_orders(client, order)[0]
    iso_date = _parse_date(date)
    delivery, meals = _resolve_day(client, current, iso_date)
    day_meal = resolve_slot(meals, slot)

    alternatives = client.get_switch_options(
        current.order_id,  # type: ignore[arg-type]
        delivery.delivery_id,  # type: ignore[arg-type]
        day_meal.delivery_meal_id,  # type: ignore[arg-type]
    )

    if json_output:
        _dump_json(
            {
                "orderId": current.order_id,
                "date": iso_date,
                "slot": day_meal.meal_name,
                "currentMealId": day_meal.diet_calories_meal_id,
                "options": [_dump_model(option) for option in alternatives],
            }
        )
        return

    typer.echo(f"{_order_line(current)} — {iso_date}")
    typer.echo(
        f"Slot: {day_meal.meal_name} (slot id {day_meal.delivery_meal_id}) — "
        f"current: {day_meal.menu_meal_name} (meal id {day_meal.diet_calories_meal_id})"
    )
    typer.echo("")
    rows = [
        [
            option.diet_option_name,
            option.menu_meal_name,
            option.diet_calories_meal_id,
            _kcal(option),
            _yes_no(option.can_be_changed),
            _yes_no(option.meal_recommended),
            _allergens_text(option.allergens),
        ]
        for option in alternatives
    ]
    typer.echo(
        _table(
            ["OPTION", "DISH", "MEAL ID", "KCAL", "CHANGEABLE", "RECOMMENDED", "ALLERGENS"],
            rows,
        )
    )


@app.command()
def switch(
    date: str = typer.Option(..., "--date", help="Delivery date, YYYY-MM-DD (or today/tomorrow)."),
    slot: str = typer.Option(..., "--slot", help='Slot name as used by Dietly, e.g. "Obiad".'),
    to: int = typer.Option(
        ..., "--to", help="Target dietCaloriesMealId (see `dietly-mcp options`)."
    ),
    order: int = ORDER_OPTION,
    apply: bool = typer.Option(
        False, "--apply", help="Actually perform the switch. Without it, nothing is sent."
    ),
    amount: int = typer.Option(1, "--amount", help="Number of portions (API expects 1)."),
    json_output: bool = JSON_OPTION,
) -> None:
    """Switch one slot to another meal — DRY RUN unless --apply is given."""
    client, settings = _client()
    iso_date = _parse_date(date)
    payload = switch_operation(
        client,
        date=iso_date,
        slot=slot,
        to_id=to,
        order_id=order,
        dry_run=not apply,
        amount=amount,
        tool="switch",
        audit=default_audit_log(settings),
    )
    if not payload.get("ok"):
        raise DietlyError(payload.get("error") or "The switch failed.")

    if not apply:
        if json_output:
            _dump_json(_switch_plan_json(payload))
            return
        _print_switch_plan(payload)
        return

    if json_output:
        out: dict[str, Any] = dict(payload.get("result") or {})
        out["date"] = payload.get("date")
        out["applied"] = True
        out["auditId"] = payload.get("auditId")
        _dump_json(out)
        return

    from_ref = payload.get("from") or {}
    to_ref = payload.get("to") or {}
    suffix = f" [audit {payload['auditId']}]" if payload.get("auditId") else ""
    typer.echo(
        f"Switched {payload.get('slot')} on {payload.get('date')}: "
        f"{from_ref.get('id')} ({from_ref.get('name') or '?'}) → "
        f"{to_ref.get('id')} ({to_ref.get('name') or '?'}).{suffix}"
    )


@app.command()
def revert(
    audit_id: str = typer.Option(
        None,
        "--audit-id",
        help="Audit entry id to revert (default: the most recent applied switch).",
    ),
    date: str = typer.Option(
        None,
        "--date",
        help="Only revert a switch on this date (YYYY-MM-DD / today / tomorrow).",
    ),
    slot: str = typer.Option(
        None, "--slot", help="Only revert a switch of this slot (e.g. Kolacja)."
    ),
    order: int = ORDER_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the revert without sending anything."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Undo a recorded switch — a REAL write (use --dry-run to preview).

    Without ``--audit-id`` the most recent applied switch from the audit log is
    reverted, optionally narrowed with ``--date``/``--slot``. The revert is
    refused when the recorded dish is no longer offered or the day looks locked.
    """
    client, settings = _client()
    payload = revert_operation(
        client,
        audit_id=audit_id or None,
        date=date or None,
        slot=slot or None,
        order_id=order,
        dry_run=dry_run,
        tool="revert",
        audit=default_audit_log(settings),
    )
    if not payload.get("ok"):
        raise DietlyError(payload.get("error") or "The revert failed.")

    if json_output:
        _dump_json(payload)
        return

    from_ref = payload.get("from") or {}
    to_ref = payload.get("to") or {}
    lines = []
    if dry_run:
        lines.append("DRY RUN — nothing was sent to Dietly.")
    lines.append(
        f"{'Would revert' if dry_run else 'Reverted'} {payload.get('slot')} "
        f"on {payload.get('date')}: {from_ref.get('id')} "
        f"({from_ref.get('name') or '?'}) → {to_ref.get('id')} "
        f"({to_ref.get('name') or '?'})."
    )
    lines.append(f"  Undid switch: audit {payload.get('revertedAuditId')}")
    if payload.get("auditId"):
        lines.append(f"  Revert audit: {payload['auditId']}")
    typer.echo("\n".join(lines))


def _audit_ref(ref: Any) -> str:
    """Render an audit ``from``/``to`` selection compactly."""
    if not isinstance(ref, dict) or ref.get("id") is None:
        return "-"
    return f"{ref.get('id')} {ref.get('name') or '?'}"


@app.command()
def audit(
    limit: int = typer.Option(
        20, "--limit", "-n", help="Show at most this many of the most recent entries."
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Show the write audit log (append-only JSONL; newest shown last)."""
    settings = load_settings()
    entries = default_audit_log(settings).entries(limit=limit)

    if json_output:
        _dump_json(entries)
        return
    if not entries:
        typer.echo(f"No audit entries yet ({settings.audit_path}).")
        return

    rows = [
        [
            str(entry.get("timestamp") or "")[:19],
            entry.get("event"),
            entry.get("result"),
            _yes_no(bool(entry.get("dry_run"))),
            entry.get("date"),
            entry.get("slot"),
            f"{_audit_ref(entry.get('from'))} → {_audit_ref(entry.get('to'))}",
            entry.get("tool"),
            str(entry.get("id") or "")[:8],
        ]
        for entry in entries
    ]
    typer.echo(
        _table(
            ["TIME (UTC)", "EVENT", "RESULT", "DRY", "DATE", "SLOT", "FROM → TO", "TOOL", "ID"],
            rows,
        )
    )
    typer.echo(f"\n{len(entries)} entr(y/ies) from {settings.audit_path}")


@app.command()
def version() -> None:
    """Print the dietly-mcp version."""
    typer.echo(f"dietly-mcp {__version__}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_cli(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and convert expected errors into single-line messages.

    Args:
        argv: arguments without the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (0 on success).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        result = app(args=args, standalone_mode=False)
    except DietlyError as exc:
        _err(f"error: {exc}")
        return 1
    except typer.Abort:
        _err("error: aborted")
        return 1
    except typer.TyperException as exc:  # usage errors, e.g. a missing --date
        _err(f"error: {exc.format_message()}")
        return int(getattr(exc, "exit_code", 1) or 1)
    return int(result) if isinstance(result, int) else 0


def main() -> None:
    """Console script entry point: run the CLI without tracebacks for expected errors."""
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
