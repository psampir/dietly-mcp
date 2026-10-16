"""Read/write client for the Dietly private JSON API.

All endpoints are called on ``https://dietly.pl/api/...`` with the cookie session
created by :mod:`dietly_mcp.session`. The only write operation this phase
supports is a single meal switch, and it is dry-run by default.

Reverse-engineered call contract: see ``docs/api-notes.md``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from .errors import (
    ApiError,
    NetworkError,
    NotFoundError,
    UnknownMealOptionError,
)
from .models import (
    DayMeal,
    Delivery,
    DeliveryMeal,
    MealOption,
    Order,
    OrderDetail,
    SwitchResult,
)
from .session import Session, api_headers, check_response

__all__ = ["DietlyClient", "resolve_delivery", "resolve_slot"]

#: Guard against a runaway pagination loop.
_MAX_PAGES = 50

#: Keys under which list payloads appear (checked in order).
_LIST_KEYS = {
    "orders": ("content", "profileOrders", "orders", "items", "results", "data"),
    "deliveries": ("deliveries", "items", "data"),
    "meals": ("deliveryMenuMeal", "meals", "items", "data"),
    "options": ("mealChangeOptions", "options", "items", "data"),
}


def _as_dict(payload: Any) -> dict[str, Any]:
    """Return ``payload`` as a dict, unwrapping common single-key wrappers."""
    if not isinstance(payload, dict):
        return {}
    for wrapper in ("data", "result", "order", "profileOrder"):
        child = payload.get(wrapper)
        if isinstance(child, dict) and wrapper not in payload:
            return child
    return payload


def _first_list(payload: Any, keys: Iterable[str]) -> list[Any] | None:
    """Find the first list value under any of ``keys`` (one level deep)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for wrapper in ("data", "result", "order"):
        child = payload.get(wrapper)
        if isinstance(child, dict):
            found = _first_list(child, keys)
            if found is not None:
                return found
    return None


def resolve_delivery(detail: OrderDetail, day: str) -> Delivery:
    """Return the delivery of ``detail`` matching ISO date ``day``.

    Raises:
        NotFoundError: the order has no delivery on that date.
    """
    for delivery in detail.deliveries:
        if (delivery.date or "")[:10] == day:
            return delivery
    available = detail.delivery_dates
    shown = ", ".join(available[:10]) or "none"
    more = f" (+{len(available) - 10} more)" if len(available) > 10 else ""
    raise NotFoundError(
        f"Order {detail.order_id} has no delivery on {day}. Available dates: {shown}{more}."
    )


def resolve_slot(day_meals: Sequence[DayMeal], slot: str) -> DayMeal:
    """Return the meal whose ``mealName`` matches ``slot`` (case-insensitive).

    Polish slot names (``Obiad``, ``II Śniadanie``, ...) are compared verbatim
    apart from surrounding whitespace and letter case.

    Raises:
        NotFoundError: no slot with that name exists on that day.
    """
    wanted = slot.strip().casefold()
    for meal in day_meals:
        if (meal.meal_name or "").strip().casefold() == wanted:
            return meal
    available = ", ".join(sorted({m.meal_name or "?" for m in day_meals})) or "none"
    raise NotFoundError(f"No meal slot named {slot!r} on that day. Slots: {available}.")


class DietlyClient:
    """Thin, typed wrapper around the Dietly API.

    Args:
        session: an authenticated :class:`~dietly_mcp.session.Session`.
        settings: optional settings override (defaults to the session's).
        company_id: catering slug used when an order's own slug is unknown.
    """

    def __init__(
        self,
        session: Session,
        *,
        settings: Any | None = None,
        company_id: str | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or session.settings
        self.default_company_id = company_id or self.settings.company_id
        self._orders: list[Order] | None = None

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        company_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an API call, map errors and return the parsed JSON body."""
        url = f"{self.settings.base_url}{path}"
        headers = api_headers(self.settings, company_id or self.default_company_id)
        try:
            response = self.session.http.request(method, url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise NetworkError(f"Could not reach {url}: {exc}.") from exc
        check_response(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                f"{url} returned a non-JSON body (status {response.status_code}).",
                status_code=response.status_code,
                url=url,
            ) from exc

    def _require_list(self, payload: Any, kind: str, *, url: str | None = None) -> list[Any]:
        """Extract the expected list from ``payload`` or raise :class:`ApiError`."""
        found = _first_list(payload, _LIST_KEYS[kind])
        if found is None:
            raise ApiError(
                f"Unexpected Dietly response shape: no {kind} list found"
                + (f" in {url}." if url else "."),
                url=url,
                payload=payload,
            )
        return found

    # ------------------------------------------------------------------ #
    # Company id (catering slug) resolution
    # ------------------------------------------------------------------ #
    def company_id_for(self, order_id: int) -> str:
        """Return the catering slug to use for ``order_id``.

        The active-orders list exposes each order's catering, so we prefer it and
        fall back to the configured default slug.
        """
        for order in self._cached_orders():
            if order.order_id == order_id and order.company_name:
                return order.company_name
        return self.default_company_id

    def _cached_orders(self) -> list[Order]:
        if self._orders is None:
            return self.get_orders(active_only=False)
        return self._orders

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        """Return orders, newest page first, optionally only active ones.

        Args:
            active_only: keep only orders whose ``status`` is ``ACTIVE``.
            refresh: ignore the internal cache and re-fetch.
        """
        if self._orders is None or refresh:
            orders: list[Order] = []
            page = 0
            total_pages = 1
            while page < total_pages and page < _MAX_PAGES:
                payload = self._request(
                    "GET", "/api/profile/profile-order/all", params={"page": page}
                )
                body = _as_dict(payload)
                items = self._require_list(payload, "orders")
                orders.extend(Order.model_validate(item) for item in items)
                raw_total = body.get("totalPages") or body.get("total_pages")
                try:
                    total_pages = int(raw_total)
                except (TypeError, ValueError):
                    total_pages = 1
                if not items:
                    break
                page += 1
            self._orders = orders
        return [o for o in self._orders if o.is_active] if active_only else list(self._orders)

    def get_active_orders(self, *, refresh: bool = False) -> list[Order]:
        """Return the active orders."""
        return self.get_orders(active_only=True, refresh=refresh)

    def get_order(self, order_id: int) -> OrderDetail:
        """Return one order with its deliveries."""
        payload = self._request(
            "GET",
            f"/api/company/customer/order/{order_id}",
            company_id=self.company_id_for(order_id),
        )
        body = _as_dict(payload)
        if "deliveries" not in body:
            nested = payload.get("order") if isinstance(payload, dict) else None
            if isinstance(nested, dict):
                body = nested
        detail = OrderDetail.model_validate(body)
        if detail.order_id is None:
            detail.order_id = order_id
        return detail

    def get_day_menu(self, order_id: int, delivery_id: int) -> list[DayMeal]:
        """Return the currently chosen meals for one delivery day (all slots)."""
        payload = self._request(
            "GET",
            f"/api/company/general/menus/delivery/{delivery_id}/new",
            company_id=self.company_id_for(order_id),
        )
        items = self._require_list(payload, "meals")
        return [DayMeal.model_validate(item) for item in items]

    def get_switch_options(
        self, order_id: int, delivery_id: int, delivery_meal_id: int
    ) -> list[MealOption]:
        """Return the alternative meals offered for one slot."""
        payload = self._request(
            "GET",
            f"/api/company/customer/order/{order_id}/deliveries/{delivery_id}"
            f"/delivery-meals/{delivery_meal_id}/switch",
            company_id=self.company_id_for(order_id),
        )
        items = self._require_list(payload, "options")
        return [MealOption.model_validate(item) for item in items]

    # ------------------------------------------------------------------ #
    # Write operation (single switch, dry-run by default)
    # ------------------------------------------------------------------ #
    def switch_meal_url(
        self,
        order_id: int,
        delivery_id: int,
        delivery_meal_id: int,
        target_id: int,
        amount: int = 1,
    ) -> str:
        """Return the exact URL a switch would PUT to (useful for logging/tests)."""
        return (
            f"{self.settings.base_url}/api/company/customer/order/{order_id}"
            f"/deliveries/{delivery_id}/delivery-meals/{delivery_meal_id}/switch"
            f"?amount={amount}&dietCaloriesMealId={target_id}"
        )

    def switch_meal(
        self,
        order_id: int,
        delivery_id: int,
        delivery_meal_id: int,
        target_id: int,
        *,
        amount: int = 1,
        dry_run: bool = True,
    ) -> SwitchResult:
        """Switch one slot to ``target_id``, or plan the switch when ``dry_run``.

        The target id is validated against the options the API actually offers
        before anything is sent (guardrail).

        Args:
            order_id: order that owns the delivery.
            delivery_id: delivery day id.
            delivery_meal_id: slot id (constant across switches).
            target_id: ``dietCaloriesMealId`` of the desired replacement.
            amount: portions, always ``1`` in practice.
            dry_run: when ``True`` (default) no request is sent.

        Raises:
            UnknownMealOptionError: ``target_id`` is not among the offered options.
        """
        day_meals = self.get_day_menu(order_id, delivery_id)
        current = next((m for m in day_meals if m.delivery_meal_id == delivery_meal_id), None)
        options = self.get_switch_options(order_id, delivery_id, delivery_meal_id)
        target = next((o for o in options if o.diet_calories_meal_id == target_id), None)
        if target is None:
            offered = ", ".join(
                f"{o.diet_calories_meal_id} "
                f"({o.diet_option_name or '?'} — {o.menu_meal_name or '?'})"
                for o in options
            )
            raise UnknownMealOptionError(
                f"Meal id {target_id} is not offered for slot {delivery_meal_id}.\n"
                f"Offered options: {offered or 'none'}."
            )

        result = SwitchResult(
            order_id=order_id,
            delivery_id=delivery_id,
            delivery_meal_id=delivery_meal_id,
            diet_calories_meal_id=target_id,
            amount=amount,
            applied=False,
            previous_diet_calories_meal_id=current.diet_calories_meal_id if current else None,
            previous_menu_meal_name=current.menu_meal_name if current else None,
            target_menu_meal_name=target.menu_meal_name,
            target_diet_option_name=target.diet_option_name,
            slot=current.meal_name if current else target.meal_name,
        )
        if dry_run:
            return result

        path = (
            f"/api/company/customer/order/{order_id}/deliveries/{delivery_id}"
            f"/delivery-meals/{delivery_meal_id}/switch"
        )
        payload = self._request(
            "PUT",
            path,
            company_id=self.company_id_for(order_id),
            params={"amount": amount, "dietCaloriesMealId": target_id},
        )
        body = _as_dict(payload) or {}
        updated = SwitchResult.model_validate(body) if body else SwitchResult()
        updated.order_id = order_id
        updated.delivery_id = delivery_id
        updated.delivery_meal_id = updated.delivery_meal_id or delivery_meal_id
        updated.diet_calories_meal_id = updated.diet_calories_meal_id or target_id
        updated.amount = updated.amount or amount
        updated.applied = True
        updated.previous_diet_calories_meal_id = result.previous_diet_calories_meal_id
        updated.previous_menu_meal_name = result.previous_menu_meal_name
        updated.target_menu_meal_name = result.target_menu_meal_name
        updated.target_diet_option_name = result.target_diet_option_name
        updated.slot = result.slot
        updated.response = body or None
        return updated

    def slot_delivery_meal(
        self, day_meals: Sequence[DayMeal], delivery_meal_id: int
    ) -> DeliveryMeal:
        """Return a lightweight :class:`DeliveryMeal` for ``delivery_meal_id``."""
        for meal in day_meals:
            if meal.delivery_meal_id == delivery_meal_id:
                return DeliveryMeal(
                    deliveryMealId=meal.delivery_meal_id,
                    dietCaloriesMealId=meal.diet_calories_meal_id,
                )
        raise NotFoundError(f"Slot {delivery_meal_id} is not part of that day's menu.")
