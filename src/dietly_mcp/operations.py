"""Shared write operations (switch / apply / revert) and their safety policy.

Both front ends call these functions — the MCP tools (``dietly_switch``,
``dietly_apply``, ``dietly_revert``) and the CLI (``switch``, ``revert``) — so the
offered-target guardrail, the dry-run semantics and the audit records cannot
drift between them.

Design rules, in order of importance:

1. **Nothing is ever sent on a dry run.** ``dry_run=True`` is the default, and a
   dry run only performs GETs.
2. **A target that Dietly did not offer is refused** before any PUT
   (:class:`~dietly_mcp.errors.UnknownMealOptionError`), and the error lists the
   valid ids.
3. **Every attempt is audited** — dry runs (``event: "dry_run"``), applies
   (``"switch"``), reverts (``"revert"``) and failures, with the previous and the
   new selection. See :mod:`dietly_mcp.audit`.
4. **Nothing raises out of an operation**: each returns a JSON-ready
   ``{"ok": bool, ...}`` payload in the same style as the read-only MCP tools, so
   the CLI and the MCP server report failures identically.

The read-only payload helpers live here too (``compact``, ``order_summary``,
``resolve_order``, ``failure_payload``) because the write path needs to describe
the same objects; :mod:`dietly_mcp.mcp_server` imports them for its read tools.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditLog, default_audit_log
from .client import DietlyClient, resolve_delivery, resolve_slot
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
    UnknownMealOptionError,
)
from .logic import (
    DEFAULT_EDIT_CUTOFF_HOURS,
    current_orders,
    is_editable,
    nutrition_kcal,
    parse_date,
)
from .models import DayMeal, MealOption, Order

__all__ = [
    "apply_operation",
    "compact",
    "failure_payload",
    "order_summary",
    "resolve_order",
    "revert_operation",
    "switch_operation",
]


# --------------------------------------------------------------------------- #
# Read-only payload helpers (shared with the read-only MCP tools)
# --------------------------------------------------------------------------- #
def compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so responses stay small."""
    return {key: value for key, value in payload.items() if value is not None}


def order_summary(order: Order, deliveries_count: int | None = None) -> dict[str, Any]:
    """Compact, JSON-ready summary of one order."""
    return compact(
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


def resolve_order(
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
        listing = [order_summary(order) for order in active]
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
        "activeOrders": [order_summary(order) for order in active],
    }


def failure_payload(exc: Exception) -> dict[str, Any]:
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
    if isinstance(exc, UnknownMealOptionError):
        return {
            "ok": False,
            "error": str(exc),
            "hint": "Call dietly_options (or `dietly-mcp options`) and use one of "
            "the listed target ids.",
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
# Small helpers
# --------------------------------------------------------------------------- #
def _coerce_id(value: Any) -> int | None:
    """Return ``value`` as an ``int`` id, or ``None`` when it is not id-like.

    Accepts an ``int`` and the common case of a digit string (a tool/CLI caller
    that only has text), rejects ``bool`` and anything else.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("+-").isdigit():
            return int(text)
    return None


def _meal_ref(model: Any) -> dict[str, Any]:
    """Describe a meal selection as ``{"id", "name", "kcal"}``."""
    return {
        "id": getattr(model, "diet_calories_meal_id", None),
        "name": getattr(model, "menu_meal_name", None),
        "kcal": nutrition_kcal(model),
    }


def _offered(option: MealOption) -> str:
    """One-line description of an offered option (id, variant, dish)."""
    return (
        f"{option.diet_calories_meal_id} "
        f"({option.diet_option_name or '?'} — {option.menu_meal_name or '?'})"
    )


def _offered_ids(options: list[MealOption]) -> str:
    """Comma-separated list of the offered target ids."""
    return ", ".join(str(o.diet_calories_meal_id) for o in options) or "none"


def _record(audit: AuditLog | None, **fields: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Append an audit record, tolerating a broken log.

    Returns ``(entry, warning)``. A failure to write the log must never lose the
    operation's own result, so ``warning`` is reported in the payload instead of
    raising.
    """
    if audit is None:
        return None, None
    try:
        return audit.record(**fields), None
    except DietlyError as exc:  # ConfigError: the audit file could not be written
        return None, str(exc)


def _audit_fields(
    entry: dict[str, Any] | None, warning: str | None
) -> dict[str, Any]:
    """Merge the audit entry id (and any warning) into a payload."""
    fields: dict[str, Any] = {"auditId": (entry or {}).get("id")}
    if warning:
        fields["auditWarning"] = warning
    return fields


def _require_order(client: DietlyClient, order_id: int | None, *, today: Any = None) -> Order:
    """Pick the order a write should target.

    The read tools list only *genuinely current* orders (status ``ACTIVE`` and
    ``dateTo >= today``), because Dietly keeps stamping finished orders as
    ``ACTIVE``. The write path prefers the same set, so an agent that picked an
    order from ``dietly_orders`` can write to it without passing ``order_id``. It
    falls back to the wider "status ACTIVE" set only when nothing is current (so
    a finished-but-ACTIVE order is still addressable, and fails cleanly later at
    the delivery-date lookup).

    Raises:
        DietlyError: no order is editable, the id is unknown, or more than one
            candidate exists and ``order_id`` was not given.
    """
    known = client.get_orders()
    current = current_orders(known, today=today)
    active = [order for order in known if order.is_active]
    if order_id is not None:
        for order in (*current, *active):
            if order.order_id == order_id:
                return order
        ids = ", ".join(str(o.order_id) for o in (*current, *active)) or "none"
        raise DietlyError(f"No order with id {order_id}. Known active order ids: {ids}.")
    if len(current) == 1:
        return current[0]
    if not current and len(active) == 1:
        return active[0]
    candidates = current or active
    if not candidates:
        raise DietlyError("No active orders found for this account.")
    listing = "\n".join(f"  - {order.label}" for order in candidates)
    raise DietlyError(f"Several orders could be edited — re-run with --order <id>:\n{listing}")


class SlotContext:
    """A resolved (order, delivery, slot, options) tuple for one day and slot."""

    __slots__ = ("date", "delivery", "day_meal", "options", "order")

    def __init__(
        self,
        *,
        order: Order,
        date: str,
        delivery: Any,
        day_meal: DayMeal,
        options: list[MealOption],
    ) -> None:
        self.order = order
        self.date = date
        self.delivery = delivery
        self.day_meal = day_meal
        self.options = options

    @property
    def order_id(self) -> int | None:
        """The owning order id."""
        return self.order.order_id

    @property
    def catering(self) -> str | None:
        """The order's catering slug."""
        return self.order.company_name

    @property
    def delivery_id(self) -> int | None:
        """The delivery day id."""
        return self.delivery.delivery_id

    @property
    def slot_name(self) -> str | None:
        """The slot name as Dietly spells it (e.g. ``"Kolacja"``)."""
        return self.day_meal.meal_name

    @property
    def delivery_meal_id(self) -> int | None:
        """The slot id (constant across switches)."""
        return self.day_meal.delivery_meal_id

    @property
    def current_ref(self) -> dict[str, Any]:
        """The currently selected meal as ``{"id", "name", "kcal"}``."""
        return _meal_ref(self.day_meal)

    def is_open(self, *, cutoff_hours: float | None = None) -> tuple[bool, bool]:
        """Whether the day can still be edited, per the API flag or the heuristic.

        Returns ``(open, is_heuristic)``. The heuristic is **not** an API
        guarantee — Dietly may still reject the write.
        """
        if self.delivery.editable is not None:
            return bool(self.delivery.editable), False
        hours = DEFAULT_EDIT_CUTOFF_HOURS if cutoff_hours is None else cutoff_hours
        return is_editable(self.delivery.date, cutoff_hours=hours), True


def _slot_context(
    client: DietlyClient,
    *,
    date: str,
    slot: str,
    order_id: int | None,
    today: Any = None,
) -> SlotContext:
    """Resolve one day + slot to its order, delivery, current meal and options.

    Raises:
        DietlyError / NotFoundError: the order, date or slot cannot be resolved.
    """
    iso = parse_date(date, today=today)
    order = _require_order(client, order_id, today=today)
    detail = client.get_order(order.order_id)  # type: ignore[arg-type]
    delivery = resolve_delivery(detail, iso)
    if delivery.delivery_id is None:
        raise DietlyError(f"Delivery on {iso} has no deliveryId in the API response.")
    meals = client.get_day_menu(order.order_id, delivery.delivery_id)  # type: ignore[arg-type]
    day_meal = resolve_slot(meals, slot)
    if day_meal.delivery_meal_id is None:
        raise DietlyError(
            f"Slot {day_meal.meal_name!r} on {iso} has no deliveryMealId in the API response."
        )
    options = client.get_switch_options(
        order.order_id,  # type: ignore[arg-type]
        delivery.delivery_id,
        day_meal.delivery_meal_id,
    )
    return SlotContext(
        order=order, date=iso, delivery=delivery, day_meal=day_meal, options=options
    )


# --------------------------------------------------------------------------- #
# The single-slot switch (the one primitive everything else builds on)
# --------------------------------------------------------------------------- #
def _switch_one(
    client: DietlyClient,
    *,
    date: Any,
    slot: Any,
    to_id: Any,
    order_id: int | None = None,
    dry_run: bool = True,
    amount: int = 1,
    tool: str | None = None,
    today: Any = None,
    audit: AuditLog | None = None,
    event: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Switch one slot, or preview it, returning a payload and auditing it.

    ``audit`` is ``None`` when the caller only wants validation (the batch
    ``apply`` pre-flight); the public entry points always pass a log.
    """
    context: dict[str, Any] = {"date": date, "slot": slot, "order_id": order_id}
    try:
        target_id = _coerce_id(to_id)
        if target_id is None:
            raise DietlyError(
                f"Invalid target meal id {to_id!r}: expected an integer id "
                "(see dietly_options / `dietly-mcp options`)."
            )
        ctx = _slot_context(client, date=date, slot=slot, order_id=order_id, today=today)
        context.update(
            {
                "date": ctx.date,
                "slot": ctx.slot_name,
                "order_id": ctx.order_id,
                "catering": ctx.catering,
                "delivery_id": ctx.delivery_id,
                "delivery_meal_id": ctx.delivery_meal_id,
            }
        )
        target = next(
            (o for o in ctx.options if o.diet_calories_meal_id == target_id), None
        )
        if target is None:
            raise UnknownMealOptionError(
                f"Meal id {target_id} is not offered for slot {ctx.slot_name!r} "
                f"on {ctx.date}.\nOffered ids: {_offered_ids(ctx.options)}.\n"
                "Options: " + ", ".join(_offered(o) for o in ctx.options)
            )
        from_ref = ctx.current_ref
        to_ref = _meal_ref(target)
        result = client.switch_meal(
            ctx.order_id,  # type: ignore[arg-type]
            ctx.delivery_id,  # type: ignore[arg-type]
            ctx.delivery_meal_id,  # type: ignore[arg-type]
            target_id,
            amount=amount,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - an operation always returns a payload
        failure = failure_payload(exc)
        entry, warning = _record(
            audit,
            event="dry_run" if dry_run else (event or "switch"),
            result="error",
            message=failure["error"],
            dry_run=dry_run,
            tool=tool,
            order_id=context.get("order_id"),
            catering=context.get("catering"),
            delivery_id=context.get("delivery_id"),
            date=context.get("date"),
            slot=context.get("slot"),
            delivery_meal_id=context.get("delivery_meal_id"),
            extra=extra,
        )
        return {
            **failure,
            "dryRun": dry_run,
            "applied": False,
            "tool": tool,
            "date": context.get("date"),
            "slot": context.get("slot"),
            "orderId": context.get("order_id"),
            **_audit_fields(entry, warning),
        }
    applied = bool(getattr(result, "applied", False)) and not dry_run
    message = (
        "Dry run — nothing was sent to Dietly."
        if dry_run
        else f"Switched {ctx.slot_name} on {ctx.date}: {from_ref['id']} "
        f"({from_ref['name']}) → {to_ref['id']} ({to_ref['name']})."
    )
    entry, warning = _record(
        audit,
        event="dry_run" if dry_run else (event or "switch"),
        result="ok",
        message=message,
        dry_run=dry_run,
        tool=tool,
        order_id=ctx.order_id,
        catering=ctx.catering,
        delivery_id=ctx.delivery_id,
        date=ctx.date,
        slot=ctx.slot_name,
        delivery_meal_id=ctx.delivery_meal_id,
        from_=from_ref,
        to=to_ref,
        extra=extra,
    )
    return {
        "ok": True,
        "dryRun": dry_run,
        "applied": applied,
        "tool": tool,
        "order": order_summary(ctx.order),
        "orderId": ctx.order_id,
        "catering": ctx.catering,
        "date": ctx.date,
        "slot": ctx.slot_name,
        "deliveryId": ctx.delivery_id,
        "deliveryMealId": ctx.delivery_meal_id,
        "from": from_ref,
        "to": {**to_ref, "variant": target.diet_option_name},
        "request": {
            "method": "PUT",
            "url": client.switch_meal_url(
                ctx.order_id,
                ctx.delivery_id,
                ctx.delivery_meal_id,
                target_id,
                amount,
            ),
        },
        "result": result.to_dict() if hasattr(result, "to_dict") else None,
        "resultSeen": getattr(result, "seen", None),
        "message": message,
        **_audit_fields(entry, warning),
    }


def switch_operation(
    client: DietlyClient,
    *,
    date: Any,
    slot: Any,
    to_id: Any,
    order_id: int | None = None,
    dry_run: bool = True,
    amount: int = 1,
    tool: str = "dietly_switch",
    today: Any = None,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    """Switch one slot to ``to_id`` — or preview it when ``dry_run`` (default).

    Refuses a target that Dietly does not offer (the error lists the valid ids)
    and never sends anything unless ``dry_run`` is ``False``. Every attempt is
    audited and the payload carries the audit entry id.
    """
    log = audit if audit is not None else default_audit_log(getattr(client, "settings", None))
    return _switch_one(
        client,
        date=date,
        slot=slot,
        to_id=to_id,
        order_id=order_id,
        dry_run=dry_run,
        amount=amount,
        tool=tool,
        today=today,
        audit=log,
    )


# --------------------------------------------------------------------------- #
# Batch apply
# --------------------------------------------------------------------------- #
def _normalize_entries(entries: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the shape of a batch, returning ``(normalized, problems)``."""
    normalized: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return [], [{"index": None, "error": "entries must be a list of objects"}]
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            problems.append(
                {"index": index, "error": "entry must be an object with date, slot and to_id"}
            )
            continue
        missing = [
            key for key in ("date", "slot", "to_id") if raw.get(key) in (None, "")
        ]
        if missing:
            problems.append(
                {"index": index, "entry": raw, "error": f"missing {', '.join(missing)}"}
            )
            continue
        try:
            iso = parse_date(str(raw["date"]))
        except ValueError as exc:
            problems.append(
                {"index": index, "entry": raw, "error": f"invalid date {raw['date']!r}: {exc}"}
            )
            continue
        target_id = _coerce_id(raw["to_id"])
        if target_id is None:
            problems.append(
                {"index": index, "entry": raw, "error": f"to_id {raw['to_id']!r} is not an id"}
            )
            continue
        normalized.append(
            {"index": index, "date": iso, "slot": str(raw["slot"]), "to_id": target_id}
        )
    return normalized, problems


def apply_operation(
    client: DietlyClient,
    *,
    entries: Any,
    order_id: int | None = None,
    dry_run: bool = True,
    tool: str = "dietly_apply",
    today: Any = None,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    """Apply a batch of switches, or preview them when ``dry_run`` (default).

    Every entry is ``{"date": "YYYY-MM-DD", "slot": "Kolacja", "to_id": 3648}``.
    All entries are validated *before* anything is sent; a batch with problems
    fails fast and returns the full list of problems instead of applying a
    partial set. A real batch applies sequentially and never aborts silently:
    each entry reports ``ok``/``error`` plus its audit id, and the payload
    summarises how many succeeded and how many failed.
    """
    log = audit if audit is not None else default_audit_log(getattr(client, "settings", None))
    normalized, problems = _normalize_entries(entries)
    if problems:
        entry, warning = _record(
            log,
            event="dry_run" if dry_run else "switch",
            result="error",
            message=f"{len(problems)} invalid entry/entries; nothing was sent.",
            dry_run=dry_run,
            tool=tool,
        )
        return {
            "ok": False,
            "dryRun": dry_run,
            "count": len(entries) if isinstance(entries, list) else 0,
            "applied": 0,
            "failed": len(problems),
            "error": f"{len(problems)} invalid entry/entries — fix them and retry; "
            "nothing was sent.",
            "problems": problems,
            "results": [],
            **_audit_fields(entry, warning),
        }
    if not normalized:
        entry, warning = _record(
            log,
            event="dry_run" if dry_run else "switch",
            result="error",
            message="No entries given.",
            dry_run=dry_run,
            tool=tool,
        )
        return {
            "ok": False,
            "dryRun": dry_run,
            "count": 0,
            "applied": 0,
            "failed": 0,
            "error": "No entries given: pass a list of {date, slot, to_id} objects.",
            "problems": [],
            "results": [],
            **_audit_fields(entry, warning),
        }

    # Pre-flight: resolve every entry (reads only) and collect all mismatches.
    invalid: list[dict[str, Any]] = []
    for item in normalized:
        probe = _switch_one(
            client,
            date=item["date"],
            slot=item["slot"],
            to_id=item["to_id"],
            order_id=order_id,
            dry_run=True,
            tool=tool,
            today=today,
            audit=None,  # the pre-flight is not an operation; it is not audited
        )
        if not probe.get("ok"):
            invalid.append(
                {
                    "index": item["index"],
                    "date": item["date"],
                    "slot": item["slot"],
                    "to_id": item["to_id"],
                    "error": probe.get("error"),
                }
            )
    if invalid:
        entry, warning = _record(
            log,
            event="dry_run" if dry_run else "switch",
            result="error",
            message=f"{len(invalid)} of {len(normalized)} entries failed validation; "
            "nothing was sent.",
            dry_run=dry_run,
            tool=tool,
        )
        return {
            "ok": False,
            "dryRun": dry_run,
            "count": len(normalized),
            "applied": 0,
            "failed": len(invalid),
            "error": f"{len(invalid)} of {len(normalized)} entries are not valid — "
            "nothing was sent. See problems.",
            "problems": invalid,
            "results": [],
            **_audit_fields(entry, warning),
        }

    results: list[dict[str, Any]] = []
    for item in normalized:
        one = _switch_one(
            client,
            date=item["date"],
            slot=item["slot"],
            to_id=item["to_id"],
            order_id=order_id,
            dry_run=dry_run,
            tool=tool,
            today=today,
            audit=log,
        )
        results.append(
            {
                "index": item["index"],
                "date": one.get("date"),
                "slot": one.get("slot"),
                "to_id": item["to_id"],
                "ok": bool(one.get("ok")),
                "applied": bool(one.get("applied")),
                "error": one.get("error"),
                "from": one.get("from"),
                "to": one.get("to"),
                "orderId": one.get("orderId"),
                "deliveryId": one.get("deliveryId"),
                **_audit_fields({"id": one.get("auditId")}, one.get("auditWarning")),
            }
        )

    succeeded = sum(1 for row in results if row["ok"])
    failed = len(results) - succeeded
    applied = sum(1 for row in results if row["applied"])
    verb = "would apply" if dry_run else "applied"
    return {
        "ok": failed == 0,
        "dryRun": dry_run,
        "count": len(results),
        "applied": applied,
        "failed": failed,
        "summary": f"{succeeded} of {len(results)} entr(y/ies) {verb} successfully"
        + (f", {failed} failed." if failed else "."),
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Revert
# --------------------------------------------------------------------------- #
def revert_operation(
    client: DietlyClient,
    *,
    audit_id: str | None = None,
    date: str | None = None,
    slot: str | None = None,
    order_id: int | None = None,
    dry_run: bool = False,
    tool: str = "dietly_revert",
    today: Any = None,
    audit: AuditLog | None = None,
) -> dict[str, Any]:
    """Undo a recorded switch, by audit id or the most recent one.

    The original selection is read back from the audit log. The revert is only
    performed when that meal is still offered *and* the day still looks open;
    otherwise a clear error is returned and nothing is sent. A successful revert
    is itself audited (``event: "revert"``), so it can be re-applied by hand.

    Args:
        audit_id: revert exactly this audit entry (must be an applied switch).
        date: restrict "most recent" to switches on this date.
        slot: restrict "most recent" to switches of this slot.
        order_id: override the recorded order id.
        dry_run: preview the revert without sending anything.
    """
    log = audit if audit is not None else default_audit_log(getattr(client, "settings", None))
    iso_filter: str | None = None
    try:
        if date:
            iso_filter = parse_date(date, today=today)
        if audit_id:
            source = log.get(audit_id)
            if source is None:
                raise DietlyError(
                    f"No audit entry with id {audit_id}. Run `dietly-mcp audit` to list entries."
                )
            if (
                source.get("event") != "switch"
                or source.get("result") != "ok"
                or source.get("dry_run")
            ):
                raise DietlyError(
                    f"Audit entry {audit_id} is not an applied switch "
                    f"(event={source.get('event')!r}, result={source.get('result')!r}, "
                    f"dryRun={source.get('dry_run')!r}); nothing to revert."
                )
        else:
            source = log.last_applied(date=iso_filter, slot=slot)
            if source is None:
                scope = ""
                if iso_filter or slot:
                    scope = f" for {iso_filter or 'any date'} / {slot or 'any slot'}"
                raise DietlyError(
                    f"No applied switch found in the audit log{scope}. Nothing to revert."
                )

        source_date = str(source.get("date") or "")[:10]
        source_slot = source.get("slot") or ""
        target_id = _coerce_id((source.get("from") or {}).get("id"))
        if target_id is None:
            raise DietlyError(
                f"Audit entry {source.get('id')} has no recorded previous meal id; "
                "revert it by hand with `dietly-mcp switch`."
            )

        ctx = _slot_context(
            client,
            date=source_date,
            slot=source_slot,
            order_id=order_id or source.get("order_id"),
            today=today,
        )
        cutoff = getattr(getattr(client, "settings", None), "edit_cutoff_hours", None)
        is_open, heuristic = ctx.is_open(cutoff_hours=cutoff)
        if not is_open:
            raise DietlyError(
                f"{ctx.date} is past the editing cutoff, so {source_slot} cannot be "
                "reverted automatically."
                + (
                    " (This is the heuristic editable flag, not an API guarantee — "
                    "`dietly-mcp switch` may still work by hand.)"
                    if heuristic
                    else " Dietly reports the day as locked."
                )
            )
        if target_id not in {o.diet_calories_meal_id for o in ctx.options}:
            raise UnknownMealOptionError(
                f"The recorded previous meal id {target_id} is not offered for "
                f"slot {ctx.slot_name!r} on {ctx.date} anymore.\n"
                f"Offered ids: {_offered_ids(ctx.options)}."
            )

        one = _switch_one(
            client,
            date=ctx.date,
            slot=ctx.slot_name,
            to_id=target_id,
            order_id=ctx.order_id,
            dry_run=dry_run,
            tool=tool,
            today=today,
            audit=log,
            event="revert",
            extra={"revertedAuditId": source.get("id")},
        )
    except Exception as exc:  # noqa: BLE001 - an operation always returns a payload
        failure = failure_payload(exc)
        entry, warning = _record(
            log,
            event="dry_run" if dry_run else "revert",
            result="error",
            message=failure["error"],
            dry_run=dry_run,
            tool=tool,
            date=date,
            slot=slot,
            extra={"revertedAuditId": audit_id} if audit_id else None,
        )
        return {
            **failure,
            "dryRun": dry_run,
            "applied": False,
            "tool": tool,
            "date": date,
            "slot": slot,
            **_audit_fields(entry, warning),
        }

    one["tool"] = tool
    one["revertedAuditId"] = source.get("id")
    one["revertedSwitch"] = {
        "date": source_date,
        "slot": source_slot,
        "from": source.get("from"),
        "to": source.get("to"),
    }
    one["dayOpen"] = {"open": is_open, "editableIsHeuristic": heuristic}
    return one
