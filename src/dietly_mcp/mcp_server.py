"""Read-only MCP server for Dietly, built on the official Python MCP SDK.

The server exposes the *inspection* half of :class:`dietly_mcp.client.DietlyClient`
over MCP stdio: session/order status, upcoming delivery days, the meals chosen
for one day and the alternatives offered for one slot. It deliberately exposes
**no write tool** — switching a meal is left to the CLI (``dietly-mcp switch``)
and a later phase.

The tools return compact JSON-serialisable dicts. Every failure that this
package can describe (expired session, rotated CSRF token, catering mismatch,
network problem, wrong date/slot) becomes an ``{"ok": false, "error", "hint"}``
object instead of a traceback, so an LLM client always gets something it can
act on. Secrets are never included in any response.

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
from .errors import (
    CateringMismatchError,
    CloudflareBlockedError,
    CsrfTokenError,
    DietlyError,
    InvalidCredentialsError,
    MissingCredentialsError,
    MissingSessionError,
    NetworkError,
    NotFoundError,
    SessionExpiredError,
)
from .logic import (
    allergen_labels,
    current_orders,
    is_editable,
    nutrition_kcal,
    parse_date,
    today_iso,
    upcoming_deliveries,
)
from .models import Order
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

_READ_ONLY_INSTRUCTIONS = (
    "Read-only tools for a Dietly (dietly.pl) account: session and active-order "
    "status, upcoming delivery days, the meals chosen for one day, and the "
    "alternatives offered for a single slot. All tools are read-only and never "
    "modify the account. Polish meal and slot names (e.g. \"Obiad\") are returned "
    "verbatim — never translated. Call dietly_status first to check the session."
)

server = _MCPServerType("dietly", instructions=_READ_ONLY_INSTRUCTIONS)


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
# Error mapping
# --------------------------------------------------------------------------- #
def _failure(exc: Exception) -> dict[str, Any]:
    """Turn an exception into a compact, actionable result (never a traceback)."""
    if isinstance(exc, ValueError):
        return {
            "ok": False,
            "error": f"Invalid date argument: {exc}.",
            "hint": "Pass YYYY-MM-DD, or the words 'today' or 'tomorrow'.",
        }
    if isinstance(exc, MissingCredentialsError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Create ~/.config/dietly-mcp/credentials.env (see the message) "
            "or export DIETLY_EMAIL / DIETLY_PASSWORD.",
        }
    if isinstance(exc, InvalidCredentialsError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Login was refused: check ~/.config/dietly-mcp/credentials.env.",
        }
    if isinstance(exc, (MissingSessionError, SessionExpiredError)):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Run `dietly-mcp login` to (re)create the session, then retry.",
        }
    if isinstance(exc, CsrfTokenError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Dietly rotated its CSRF token. Retry once; if it persists, "
            "run `dietly-mcp login`.",
        }
    if isinstance(exc, CateringMismatchError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Wrong order/catering. Re-check the order id with dietly_orders.",
        }
    if isinstance(exc, CloudflareBlockedError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Cloudflare challenged the request; wait a minute and retry.",
        }
    if isinstance(exc, NetworkError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Network problem reaching Dietly; check connectivity and retry.",
        }
    if isinstance(exc, NotFoundError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "The requested date, slot or order was not found on that order.",
        }
    if isinstance(exc, DietlyError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Dietly returned an unexpected error; see the message.",
        }
    return {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "hint": "Unexpected error inside the Dietly MCP server.",
    }


# --------------------------------------------------------------------------- #
# Payload builders (pure: they only call the read methods of the client)
# --------------------------------------------------------------------------- #
def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so responses stay small."""
    return {key: value for key, value in payload.items() if value is not None}


def _order_summary(order: Order, deliveries_count: int | None = None) -> dict[str, Any]:
    """Compact, JSON-ready summary of one order."""
    return _compact(
        {
            "orderId": order.order_id,
            "catering": order.company_name,
            "cateringName": order.company_display_name,
            "diet": order.diet_name,
            "kcal": order.diet_calories,
            "dateFrom": (order.date_from or "")[:10] or None,
            "dateTo": (order.date_to or "")[:10] or None,
            "deliveriesCount": deliveries_count,
            "mealsPerDay": order.meal_count,
        }
    )


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


def _resolve_order(
    client: DietlyClient, order_id: int | None, *, today: Any = None
) -> tuple[Order | None, dict[str, Any] | None]:
    """Pick the order to act on.

    Returns ``(order, None)`` on success or ``(None, failure_payload)`` when the
    order is ambiguous, unknown or no longer active — the caller then returns the
    failure payload unchanged.
    """
    active = current_orders(client.get_orders(), today=today)
    if order_id is not None:
        for order in active:
            if order.order_id == order_id:
                return order, None
        known = client.get_orders()
        listing = [_order_summary(order) for order in active]
        if any(order.order_id == order_id for order in known):
            return None, {
                "ok": False,
                "error": f"Order {order_id} is not currently active "
                "(its delivery window has ended).",
                "activeOrders": listing,
            }
        return None, {
            "ok": False,
            "error": f"No order with id {order_id}.",
            "activeOrders": listing,
        }
    if len(active) == 1:
        return active[0], None
    if not active:
        return None, {
            "ok": False,
            "error": "No active orders found for this account.",
            "activeOrders": [],
        }
    return None, {
        "ok": False,
        "error": "Several active orders — pass order_id to choose one.",
        "activeOrders": [_order_summary(order) for order in active],
    }


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
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    """Run the MCP server over stdio (the transport OpenCode spawns)."""
    server.run("stdio")


if __name__ == "__main__":
    main()
