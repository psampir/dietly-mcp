"""Pure date/selection helpers shared by the CLI and the MCP tools.

Nothing in this module performs I/O: every function takes models (or plain
values) and returns new ones, so the CLI and the MCP server apply exactly the
same policy and both are trivially testable.

Two policy decisions live here:

* **"real" activity.** Dietly keeps ``status: "ACTIVE"`` on orders whose end
  date already passed, so a listing filtered by status alone still shows closed
  orders. :func:`is_order_current` additionally requires ``dateTo >= today``.
* **the editable heuristic.** The live API did not expose a reliable
  editable flag, so :func:`is_editable` guesses it from the delivery date and a
  configurable cutoff (``DIETLY_EDIT_CUTOFF_HOURS``, default 36). It is a
  *heuristic*: it can be wrong in both directions, and a write may still be
  rejected by Dietly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from typing import Any

from .models import Delivery, Order

__all__ = [
    "DEFAULT_EDIT_CUTOFF_HOURS",
    "allergen_labels",
    "coerce_date",
    "current_orders",
    "is_editable",
    "is_order_current",
    "nutrition_kcal",
    "parse_date",
    "sort_deliveries",
    "sort_orders",
    "today_iso",
    "upcoming_deliveries",
]

#: Deliveries closer than this are assumed to be locked by Dietly.
DEFAULT_EDIT_CUTOFF_HOURS = 36.0


# --------------------------------------------------------------------------- #
# Date parsing
# --------------------------------------------------------------------------- #
def _today(today: date_cls | None = None) -> date_cls:
    """Return ``today`` or the current local date."""
    return today or date_cls.today()


def today_iso(today: date_cls | None = None) -> str:
    """Return today's date as ``YYYY-MM-DD`` (injectable for tests)."""
    return _today(today).isoformat()


def parse_date(value: str, *, today: date_cls | None = None) -> str:
    """Normalise a user-supplied date to ``YYYY-MM-DD``.

    Accepts an ISO date plus the words ``today`` and ``tomorrow``.

    Raises:
        ValueError: the value is not one of the accepted forms.
    """
    text = (value or "").strip().lower()
    if text == "today":
        return _today(today).isoformat()
    if text == "tomorrow":
        return (_today(today) + timedelta(days=1)).isoformat()
    return date_cls.fromisoformat(text).isoformat()


def coerce_date(value: Any) -> date_cls | None:
    """Interpret an API date string (or ``date``/``datetime``) as a date.

    API timestamps such as ``"2026-06-15T00:00:00Z"`` are truncated to their
    date part. Returns ``None`` when the value is missing or unparseable.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_cls):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date_cls.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Editable heuristic
# --------------------------------------------------------------------------- #
def is_editable(
    date_str: str | None,
    *,
    now: datetime | None = None,
    cutoff_hours: float = DEFAULT_EDIT_CUTOFF_HOURS,
) -> bool:
    """Guess whether a delivery on ``date_str`` can still be edited.

    A delivery counts as editable when it starts at least ``cutoff_hours``
    after ``now`` (the delivery day is treated as starting at midnight local
    time). This mirrors Dietly's behaviour of locking a day roughly 1.5 days
    ahead, but it is only a *heuristic* — the server is the final authority and
    may still reject a write.

    Args:
        date_str: delivery date (``YYYY-MM-DD``, optionally with a time part).
        now: reference instant; defaults to :func:`datetime.now`.
        cutoff_hours: how far ahead a delivery must be to be editable.
    """
    day = coerce_date(date_str)
    if day is None:
        return False
    reference = now or datetime.now()
    start = datetime.combine(day, time.min, tzinfo=reference.tzinfo)
    return start - reference >= timedelta(hours=cutoff_hours)


# --------------------------------------------------------------------------- #
# Order activity (the "ACTIVE but historical" quirk)
# --------------------------------------------------------------------------- #
def is_order_current(order: Order, *, today: date_cls | None = None) -> bool:
    """Whether ``order`` is genuinely active *now*.

    Requires ``status == "ACTIVE"`` **and** a missing or non-past ``dateTo``.
    Dietly marks finished orders as ``ACTIVE`` too, so the date check is what
    actually filters history out.
    """
    if not order.is_active:
        return False
    end = coerce_date(order.date_to)
    if end is None:  # no end date known → trust the status
        return True
    return end >= _today(today)


def current_orders(orders: Iterable[Order], *, today: date_cls | None = None) -> list[Order]:
    """Return the genuinely-active orders, sorted by start date."""
    return sort_orders([order for order in orders if is_order_current(order, today=today)])


def _sort_key_start(order: Order) -> tuple[int, str]:
    """Sort orders by ``dateFrom`` ascending, undated ones last."""
    start = coerce_date(order.date_from)
    return (1, "") if start is None else (0, start.isoformat())


def sort_orders(orders: Iterable[Order]) -> list[Order]:
    """Return orders sorted by start date (ascending)."""
    return sorted(orders, key=_sort_key_start)


# --------------------------------------------------------------------------- #
# Deliveries
# --------------------------------------------------------------------------- #
def _sort_key_date(delivery: Delivery) -> tuple[int, str]:
    """Sort deliveries by date ascending, undated ones last."""
    day = coerce_date(delivery.date)
    return (1, "") if day is None else (0, day.isoformat())


def sort_deliveries(deliveries: Iterable[Delivery]) -> list[Delivery]:
    """Return deliveries sorted by date (ascending)."""
    return sorted(deliveries, key=_sort_key_date)


def upcoming_deliveries(
    deliveries: Sequence[Delivery], *, today: date_cls | None = None
) -> list[Delivery]:
    """Return deliveries on or after today, sorted by date."""
    limit = _today(today)
    keep = [d for d in deliveries if (day := coerce_date(d.date)) and day >= limit]
    return sort_deliveries(keep)


# --------------------------------------------------------------------------- #
# Meal presentation helpers (raw API values, never translated)
# --------------------------------------------------------------------------- #
def allergen_labels(entries: Iterable[Any] | None) -> list[str]:
    """Return allergen labels from raw API entries (kept verbatim).

    Entries may be plain strings or dicts carrying one of ``name``,
    ``allergenName`` or ``code``.
    """
    labels: list[str] = []
    for entry in entries or []:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                labels.append(text)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("allergenName") or entry.get("code")
            text = str(name).strip() if name is not None else ""
            if text:
                labels.append(text)
    return labels


def nutrition_kcal(model: Any) -> float | int | None:
    """Best-effort kcal figure from a model's ``nutrition`` dict, if any."""
    nutrition = getattr(model, "nutrition", None)
    if not isinstance(nutrition, dict):
        return None
    for key in ("calories", "kcal", "energyKcal", "energy", "caloriesKcal"):
        value = nutrition.get(key)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None
