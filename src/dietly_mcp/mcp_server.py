"""MCP server for Dietly, built on the official Python MCP SDK.

The server exposes two halves of :class:`dietly_mcp.client.DietlyClient` over MCP
stdio:

* **read tools** — session/order status, upcoming delivery days, the meals chosen
  for one day and the alternatives offered for one slot;
* **write tools** — ``dietly_switch``, ``dietly_apply`` and ``dietly_revert``,
  which change the user's **real** Dietly order.

Every write tool is **dry-run by default** and only touches the account when it
is called with an explicit ``dry_run=False``; each attempt (dry run, apply or
revert, success or failure) is appended to the JSONL audit log
(``~/.local/state/dietly-mcp/audit.jsonl``, ``DIETLY_AUDIT_FILE``) described in
:mod:`dietly_mcp.audit`. The shared implementation lives in
:mod:`dietly_mcp.operations`, which the CLI uses too, so behaviour cannot drift.

The tools return compact JSON-serialisable dicts. Every failure that this
package can describe (expired session, rotated CSRF token, catering mismatch,
network problem, wrong date/slot, a target that was not offered) becomes an
``{"ok": false, "error", "hint"}`` object instead of a traceback, so an LLM
client always gets something it can act on. Secrets are never included in any
response.

SDK note: ``mcp`` 2.x renamed ``FastMCP`` to ``MCPServer``
(``from mcp.server.mcpserver import MCPServer``); the import below falls back to
the v1 ``FastMCP`` name so either major works.
"""

from __future__ import annotations

import contextlib
from typing import Any

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _MCPServerType
except ImportError:  # pragma: no cover - only hit with mcp < 2
    from mcp.server.fastmcp import FastMCP as _MCPServerType

from .client import DietlyClient, resolve_delivery, resolve_slot
from .config import Settings, load_credentials, load_settings
from .errors import DietlyError, MissingSessionError, SessionExpiredError
from .logic import (
    allergen_labels,
    current_orders,
    is_editable,
    nutrition_kcal,
    parse_date,
    today_iso,
    upcoming_deliveries,
)
from .operations import (
    apply_operation,
    revert_operation,
    switch_operation,
)
from .operations import (
    compact as _compact,
)
from .operations import (
    failure_payload as _failure,
)
from .operations import (
    order_summary as _order_summary,
)
from .operations import (
    resolve_order as _resolve_order,
)
from .session import Session

__all__ = [
    "main",
    "server",
    "day_payload",
    "deliveries_payload",
    "options_payload",
    "orders_payload",
    "status_payload",
]

_INSTRUCTIONS = (
    "Tools for a Dietly (dietly.pl) account. Read tools report session and "
    "active-order status, upcoming delivery days, the meals chosen for one day, "
    "and the alternatives offered for a single slot. The write tools "
    "(dietly_switch, dietly_apply, dietly_revert) MODIFY THE USER'S REAL ORDER: "
    "they are dry-run by default and only write when called with "
    "dry_run=False. Always call a write tool with the default dry_run=True "
    "first, show the returned preview (current dish → target dish, ids, kcal, "
    "order/date/slot) to the user, and get explicit approval before repeating the "
    "call with dry_run=False. Every attempt is recorded in the audit log and can "
    "be undone with dietly_revert. The per-day edit cutoff is a heuristic and "
    "Dietly may still reject a write. Polish meal and slot names (e.g. \"Obiad\") "
    "are returned verbatim — never translated. Call dietly_status first to check "
    "the session."
)

server = _MCPServerType("dietly", instructions=_INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# Client lifecycle (lazy, with automatic login)
# --------------------------------------------------------------------------- #
_CLIENT: DietlyClient | None = None


def _reset_client() -> None:
    """Drop the cached client (used when the session turns out to be expired)."""
    global _CLIENT
    if _CLIENT is not None:
        with contextlib.suppress(Exception):
            _CLIENT.session.close()
    _CLIENT = None


def _load_or_login(settings: Settings) -> Session:
    """Return a valid session, logging in from the credentials file if needed.

    A stored session is reused when it still verifies. If it is missing or has
    expired, the credentials file is used for a fresh login (which also
    refreshes the on-disk cookie jar).
    """
    try:
        session = Session.load(settings)
    except MissingSessionError:
        session = None
    else:
        try:
            session.verify()
        except (MissingSessionError, SessionExpiredError):
            session.close()
            session = None
        else:
            return session

    credentials = load_credentials(settings.credentials_path)
    return Session.login(credentials, settings)


def _connect() -> DietlyClient:
    """Build a client on a fresh, verified session."""
    settings = load_settings()
    return DietlyClient(_load_or_login(settings), settings=settings)


def _client() -> DietlyClient:
    """Return the cached client, connecting on first use."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _connect()
    return _CLIENT


def _run(build: Any) -> dict[str, Any]:
    """Run ``build(client)``, reconnecting once when the session has expired."""
    global _CLIENT
    client = _client()
    try:
        return build(client)
    except SessionExpiredError:
        _CLIENT = None
        with contextlib.suppress(Exception):
            client.session.close()
        return build(_client())


# --------------------------------------------------------------------------- #
# Payload builders (pure: they only call the read methods of the client)
# --------------------------------------------------------------------------- #
def _deliveries_count(client: DietlyClient, order_id: int | None) -> int | None:
    """Number of deliveries on an order, or ``None`` when it cannot be read."""
    if order_id is None:
        return None
    try:
        return len(client.get_order(order_id).deliveries)
    except SessionExpiredError:
        raise
    except DietlyError:
        return None


def status_payload(client: DietlyClient) -> dict[str, Any]:
    """Session validity plus a summary of the genuinely-active orders."""
    session = client.session
    valid = session.is_valid()
    active = current_orders(client.get_orders()) if valid else []
    payload: dict[str, Any] = {
        "ok": True,
        "session": _compact(
            {
                "valid": valid,
                "email": session.email,
                "file": str(client.settings.session_path),
            }
        ),
        "today": today_iso(),
        "activeOrders": [_order_summary(order) for order in active],
    }
    if not valid:
        payload["message"] = "Stored session is no longer valid. Run `dietly-mcp login`."
    return payload


def orders_payload(client: DietlyClient, *, today: Any = None) -> dict[str, Any]:
    """Genuinely-active orders (status ACTIVE and ``dateTo >= today``), by start date."""
    orders = current_orders(client.get_orders(), today=today)
    items = [
        _order_summary(order, _deliveries_count(client, order.order_id)) for order in orders
    ]
    return {
        "ok": True,
        "today": today_iso(today),
        "count": len(items),
        "orders": items,
    }


def deliveries_payload(
    client: DietlyClient,
    order_id: int | None = None,
    *,
    today: Any = None,
    cutoff_hours: float | None = None,
) -> dict[str, Any]:
    """Upcoming delivery days of one order, with an estimated ``editable`` flag."""
    order, failure = _resolve_order(client, order_id, today=today)
    if failure is not None:
        return failure
    assert order is not None  # _resolve_order returns one or the other
    if cutoff_hours is None:
        cutoff_hours = client.settings.edit_cutoff_hours

    detail = client.get_order(order.order_id)  # type: ignore[arg-type]
    days: list[dict[str, Any]] = []
    for delivery in upcoming_deliveries(detail.deliveries, today=today):
        heuristic = delivery.editable is None
        editable = (
            is_editable(delivery.date, cutoff_hours=cutoff_hours)
            if heuristic
            else delivery.editable
        )
        days.append(
            _compact(
                {
                    "date": (delivery.date or "")[:10] or None,
                    "deliveryId": delivery.delivery_id,
                    "editable": editable,
                    "editableIsHeuristic": heuristic,
                    "mealCount": len(delivery.delivery_meals),
                }
            )
        )

    return {
        "ok": True,
        "order": _order_summary(order),
        "today": today_iso(today),
        "count": len(days),
        "days": days,
        "editableHeuristic": {
            "cutoffHours": cutoff_hours,
            "isHeuristic": True,
            "note": "editable is estimated from the delivery date and the cutoff; "
            "Dietly is the final authority and may still reject a change.",
        },
    }


def day_payload(
    client: DietlyClient,
    date: str,
    order_id: int | None = None,
    *,
    today: Any = None,
) -> dict[str, Any]:
    """The meals currently chosen for every slot of one delivery date."""
    iso_date = parse_date(date, today=today)
    order, failure = _resolve_order(client, order_id, today=today)
    if failure is not None:
        return failure
    assert order is not None

    detail = client.get_order(order.order_id)  # type: ignore[arg-type]
    delivery = resolve_delivery(detail, iso_date)
    if delivery.delivery_id is None:
        return {
            "ok": False,
            "error": f"Delivery on {iso_date} has no deliveryId in the API response.",
        }
    meals = client.get_day_menu(order.order_id, delivery.delivery_id)  # type: ignore[arg-type]
    slots = [
        _compact(
            {
                "slot": meal.meal_name,
                "dish": meal.menu_meal_name,
                "kcal": nutrition_kcal(meal),
                "deliveryMealId": meal.delivery_meal_id,
                "dietCaloriesMealId": meal.diet_calories_meal_id,
                "switchable": meal.switchable,
                "allergens": allergen_labels(meal.allergens),
            }
        )
        for meal in meals
    ]
    return {
        "ok": True,
        "order": _order_summary(order),
        "date": iso_date,
        "deliveryId": delivery.delivery_id,
        "slotCount": len(slots),
        "slots": slots,
    }


def options_payload(
    client: DietlyClient,
    date: str,
    slot: str,
    order_id: int | None = None,
    *,
    today: Any = None,
) -> dict[str, Any]:
    """Alternatives offered for one slot of one day (current option first)."""
    iso_date = parse_date(date, today=today)
    order, failure = _resolve_order(client, order_id, today=today)
    if failure is not None:
        return failure
    assert order is not None

    detail = client.get_order(order.order_id)  # type: ignore[arg-type]
    delivery = resolve_delivery(detail, iso_date)
    if delivery.delivery_id is None:
        return {
            "ok": False,
            "error": f"Delivery on {iso_date} has no deliveryId in the API response.",
        }
    meals = client.get_day_menu(order.order_id, delivery.delivery_id)  # type: ignore[arg-type]
    day_meal = resolve_slot(meals, slot)
    current_id = day_meal.diet_calories_meal_id
    alternatives = client.get_switch_options(
        order.order_id,  # type: ignore[arg-type]
        delivery.delivery_id,
        day_meal.delivery_meal_id,  # type: ignore[arg-type]
    )
    items = [
        _compact(
            {
                "dish": option.menu_meal_name,
                "variant": option.diet_option_name,
                "targetId": option.diet_calories_meal_id,
                "kcal": nutrition_kcal(option),
                "allergens": allergen_labels(option.allergens),
                "recommended": option.meal_recommended,
                "changeable": option.can_be_changed,
                "current": option.diet_calories_meal_id == current_id,
            }
        )
        for option in alternatives
    ]
    items.sort(
        key=lambda item: (
            not item.get("current"),
            not item.get("recommended"),
            (item.get("dish") or "").casefold(),
        )
    )
    return {
        "ok": True,
        "order": _order_summary(order),
        "date": iso_date,
        "slot": day_meal.meal_name,
        "deliveryId": delivery.delivery_id,
        "deliveryMealId": day_meal.delivery_meal_id,
        "current": _compact({"dish": day_meal.menu_meal_name, "targetId": current_id}),
        "count": len(items),
        "options": items,
    }


# --------------------------------------------------------------------------- #
# Tools (read-only)
# --------------------------------------------------------------------------- #
@server.tool()
def dietly_status() -> dict[str, Any]:
    """Report the Dietly session state and a summary of the active order(s).

    Read-only. Logs in automatically from the credentials file when the stored
    session is missing or expired. Returns JSON with the session validity, the
    account email, and every order that is genuinely running now (status ACTIVE
    *and* an end date that has not passed). If login fails, returns an
    actionable {"ok": false, "error", "hint"} object instead of raising.
    """
    try:
        return _run(status_payload)
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_orders() -> dict[str, Any]:
    """List the account's currently active Dietly orders.

    Read-only. "Active" means status ACTIVE *and* dateTo on/after today — Dietly
    also stamps historical orders as ACTIVE, so that date filter is what hides
    finished orders. Sorted by start date. Each entry carries the order id, the
    catering slug and name, the diet name, kcal, the date range, the number of
    deliveries and meals per day.
    """
    try:
        return _run(orders_payload)
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_deliveries(order_id: int | None = None) -> dict[str, Any]:
    """List upcoming delivery days of an order, with an estimated editable flag.

    Read-only. Days are filtered to today or later and sorted by date. The
    `editable` flag is a HEURISTIC: a day is assumed editable when it starts at
    least DIETLY_EDIT_CUTOFF_HOURS hours from now (default 36). Dietly is the
    final authority and can still reject a change. If order_id is omitted and
    more than one order is active, a message listing them is returned instead
    of guessing.
    """
    try:
        return _run(deliveries_payload)
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_day(date: str, order_id: int | None = None) -> dict[str, Any]:
    """Show the meals currently chosen for one delivery date.

    Read-only. Returns one entry per slot (five on a full plan: Śniadanie,
    II Śniadanie, Obiad, Podwieczorek, Kolacja) with the dish name, kcal, the
    slot id (deliveryMealId), the meal id (dietCaloriesMealId) and whether the
    slot is switchable. `date` accepts YYYY-MM-DD or the words "today" or
    "tomorrow". Polish slot and dish names are returned verbatim.
    """
    try:
        return _run(lambda client: day_payload(client, date, order_id))
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_options(date: str, slot: str, order_id: int | None = None) -> dict[str, Any]:
    """List the alternative meals Dietly offers for one slot of one day.

    Read-only. For every alternative it returns the dish name, the variant/option
    name, the target id to switch to (dietCaloriesMealId), kcal, allergens and
    whether it is the currently selected option (that one comes first). `date`
    accepts YYYY-MM-DD or "today"/"tomorrow"; `slot` must match Dietly's spelling
    (e.g. "Obiad", "II Śniadanie"), matched case-insensitively.
    """
    try:
        return _run(lambda client: options_payload(client, date, slot, order_id))
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


# --------------------------------------------------------------------------- #
# Tools (write)
#
# !! These tools MODIFY THE USER'S REAL DIETLY ORDER. !!
#
# They are dry-run by default: only an explicit `dry_run=False` sends a request.
# The calling agent must first run the tool with the default `dry_run=True`,
# show the preview to the user, and obtain explicit approval before calling it
# again with `dry_run=False`. Every attempt — dry run, apply or failure — is
# appended to the JSONL audit log (`~/.local/state/dietly-mcp/audit.jsonl`, path
# overridable with DIETLY_AUDIT_FILE) and can be undone with `dietly_revert`.
# --------------------------------------------------------------------------- #
@server.tool()
def dietly_switch(
    date: str,
    slot: str,
    to_id: int | str,
    order_id: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Switch one meal slot to another dish — a WRITE to the real Dietly order.

    **This tool modifies the user's real order.** It is dry-run by default:
    nothing is sent unless you pass `dry_run=False`. Always call it once with the
    default `dry_run=True`, show the returned preview (current dish → target
    dish, both ids, kcal, and the order/date/slot context) to the user, and get
    explicit approval before repeating the call with `dry_run=False`.

    The target is refused (before anything is sent) unless it is among the
    options Dietly actually offers for that slot — the error lists the valid ids.
    With `dry_run=True` no request is sent and the payload is the preview; with
    `dry_run=False` the switch is performed and the payload adds the server
    result and the audit entry id (`auditId`).

    `date` accepts YYYY-MM-DD or "today"/"tomorrow"; `slot` must match Dietly's
    spelling (e.g. "Kolacja", "II Śniadanie"), case-insensitively; `to_id` is the
    `dietCaloriesMealId` from `dietly_options`. The per-day edit cutoff is a
    heuristic — Dietly may still reject the write. Undo with `dietly_revert`.
    """
    try:
        return _run(
            lambda client: switch_operation(
                client,
                date=date,
                slot=slot,
                to_id=to_id,
                order_id=order_id,
                dry_run=dry_run,
                tool="dietly_switch",
            )
        )
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_apply(
    entries: list[dict],
    order_id: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply several meal switches in one go — a WRITE to the real Dietly order.

    **This tool modifies the user's real order.** It is dry-run by default:
    nothing is sent unless you pass `dry_run=False`. Always run it once with the
    default `dry_run=True`, show the full preview to the user, and get explicit
    approval before repeating it with `dry_run=False`.

    `entries` is a list of `{"date": "2026-10-29", "slot": "Kolacja",
    "to_id": 3648}` objects. **All entries are validated before anything is
    sent**: a shape problem or a target that is not offered makes the whole batch
    fail fast and return the full list of `problems` (nothing is sent). With
    `dry_run=True` the payload carries one preview per entry. With
    `dry_run=False` the switches are applied sequentially and never abort
    silently: each result has `ok`/`error` plus its `auditId`, and the payload
    summarises `applied` vs `failed`. Every attempt is audited; undo individual
    switches with `dietly_revert`.
    """
    try:
        return _run(
            lambda client: apply_operation(
                client,
                entries=entries,
                order_id=order_id,
                dry_run=dry_run,
                tool="dietly_apply",
            )
        )
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


@server.tool()
def dietly_revert(
    audit_id: str | None = None,
    date: str | None = None,
    slot: str | None = None,
) -> dict[str, Any]:
    """Undo a recorded meal switch — a WRITE to the real Dietly order.

    **This tool modifies the user's real order**, but it *undoes* a switch that
    is already recorded in the audit log, which makes it the safety net for
    `dietly_switch`/`dietly_apply`: it restores the previous dish without needing
    a `dry_run` round-trip. Pass `audit_id` (from a previous write) to revert
    that exact entry, or only `date`/`slot` to revert the most recent applied
    switch (optionally filtered). The revert is refused with a clear error — and
    nothing is sent — when the recorded previous dish is no longer offered or
    the day is (heuristically) past the editing cutoff. A successful revert is
    itself audited (`event: "revert"`) and can be re-applied by hand.
    """
    try:
        return _run(
            lambda client: revert_operation(
                client,
                audit_id=audit_id,
                date=date,
                slot=slot,
                tool="dietly_revert",
            )
        )
    except Exception as exc:  # noqa: BLE001 - the tool boundary must never traceback
        return _failure(exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the MCP server over stdio (the transport OpenCode spawns)."""
    server.run("stdio")


if __name__ == "__main__":
    main()
