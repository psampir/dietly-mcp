"""Pydantic models mirroring the Dietly private JSON API.

Python attribute names are snake_case, wire names are camelCase. Every field
declares both an ``alias`` (used when serialising with ``to_dict()``) and a
``validation_alias`` (the API spelling plus tolerated variants), so models
round-trip cleanly and accept both spellings on input.

Polish API values — for example ``mealName == "Obiad"`` — are stored verbatim
and never translated.

Deep, schema-less sub-structures (``ingredients``, ``nutrition``, allergen
entries) are intentionally kept as raw dicts; only the fields this project acts
on are modelled.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _names(*names: str) -> dict[str, Any]:
    """Return ``Field`` kwargs: first name serialises, all names validate."""
    return {"alias": names[0], "validation_alias": AliasChoices(*names)}


class ApiModel(BaseModel):
    """Base model: camelCase aliases in, unknown fields preserved."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    def to_dict(self, *, camel: bool = True, exclude_none: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable dict.

        Args:
            camel: emit API-style camelCase keys (default) instead of snake_case.
            exclude_none: drop ``None`` values.
        """
        extras = set(self.model_extra or {})
        return self.model_dump(
            mode="json",
            by_alias=camel,
            exclude_none=exclude_none,
            exclude=extras or None,
        )


class Order(ApiModel):
    """One order as returned by ``/api/profile/profile-order/all``."""

    order_id: int | None = Field(default=None, **_names("orderId", "id", "profileOrderId"))
    status: str | None = None
    date_from: str | None = Field(default=None, **_names("dateFrom", "startDate"))
    date_to: str | None = Field(default=None, **_names("dateTo", "endDate"))
    company_name: str | None = Field(
        default=None, **_names("companyName", "companyId", "companySlug")
    )
    company_display_name: str | None = Field(
        default=None, **_names("companyDisplayName", "companyLabel")
    )
    diet_calories: int | None = Field(default=None, **_names("dietCalories", "calories"))
    diet_name: str | None = Field(default=None, **_names("dietName", "diet"))
    meal_count: int | None = Field(default=None, **_names("mealCount", "mealsPerDay"))

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value: Any) -> Any:
        """Accept ``"ACTIVE"`` as well as ``{"name": "ACTIVE"}`` shapes."""
        if isinstance(value, dict):
            for key in ("name", "code", "value", "status"):
                if key in value and isinstance(value[key], str):
                    return value[key]
            return None
        return value

    @property
    def is_active(self) -> bool:
        """Whether the order is currently active."""
        return (self.status or "").strip().upper() == "ACTIVE"

    @property
    def catering_slug(self) -> str | None:
        """Catering slug for this order (the ``company-id`` header value)."""
        return self.company_name

    @property
    def label(self) -> str:
        """Short label, e.g. ``"152698 · wybormenu · OPTIMAL 3000 kcal"``."""
        parts: list[str] = []
        if self.order_id is not None:
            parts.append(str(self.order_id))
        if self.company_name:
            parts.append(self.company_name)
        kcal = f"{self.diet_calories} kcal" if self.diet_calories else None
        diet = " ".join(part for part in (self.diet_name, kcal) if part)
        if diet:
            parts.append(diet)
        return " · ".join(parts) or "unknown order"


class DeliveryMeal(ApiModel):
    """A single meal slot inside a delivery day (slot id + chosen variant id)."""

    delivery_meal_id: int | None = Field(default=None, **_names("deliveryMealId", "id"))
    diet_calories_meal_id: int | None = Field(
        default=None, **_names("dietCaloriesMealId", "mealId")
    )
    amount: int | None = None


class Delivery(ApiModel):
    """A delivery day of an order."""

    delivery_id: int | None = Field(default=None, **_names("deliveryId", "id"))
    date: str | None = None
    delivery_meals: list[DeliveryMeal] = Field(
        default_factory=list, **_names("deliveryMeals", "meals")
    )
    #: ``True``/``False`` when the API exposes the editing window, else ``None``.
    editable: bool | None = Field(
        default=None, **_names("editable", "canEdit", "canBeChanged", "isEditable")
    )

    @field_validator("delivery_meals", mode="before")
    @classmethod
    def _coerce_meals(cls, value: Any) -> Any:
        """Treat a missing meal list as empty."""
        return value if value is not None else []


class OrderDetail(ApiModel):
    """``/api/company/customer/order/{orderId}`` payload."""

    order_id: int | None = Field(default=None, **_names("orderId", "id"))
    company_name: str | None = Field(default=None, **_names("companyName", "companyId"))
    deliveries: list[Delivery] = Field(default_factory=list)
    nearest_delivery: dict[str, Any] | None = Field(default=None, **_names("nearestDelivery"))

    @field_validator("deliveries", mode="before")
    @classmethod
    def _coerce_deliveries(cls, value: Any) -> Any:
        """Treat a missing delivery list as empty."""
        return value if value is not None else []

    @property
    def delivery_dates(self) -> list[str]:
        """All delivery dates: from ``deliveries[]``, else ``nearestDelivery``."""
        dates = [d.date for d in self.deliveries if d.date]
        if dates:
            return dates
        if isinstance(self.nearest_delivery, dict):
            raw = self.nearest_delivery.get("deliveryDates") or []
            return [str(item) for item in raw]
        return []


class DayMeal(ApiModel):
    """One chosen meal for a given day (a slot entry of ``deliveryMenuMeal``)."""

    meal_name: str | None = Field(default=None, **_names("mealName", "slotName"))
    menu_meal_name: str | None = Field(default=None, **_names("menuMealName", "dishName"))
    delivery_meal_id: int | None = Field(default=None, **_names("deliveryMealId", "id"))
    diet_calories_meal_id: int | None = Field(
        default=None, **_names("dietCaloriesMealId", "mealId")
    )
    switchable: bool | None = None
    allergens: list[Any] = Field(default_factory=list)
    allergens_with_excluded: list[Any] = Field(
        default_factory=list, **_names("allergensWithExcluded")
    )
    ingredients: list[dict[str, Any]] = Field(default_factory=list)
    nutrition: dict[str, Any] | None = None


class MealOption(ApiModel):
    """One switchable alternative for a slot (``mealChangeOptions`` entry).

    The API nests the dish data under ``menuMealDetails``; the validator below
    flattens it so the wrapper flags and the dish fields live on one object
    (``dietCaloriesMealId`` is the id to send when switching).
    """

    diet_option_name: str | None = Field(default=None, **_names("dietOptionName", "optionName"))
    meal_name: str | None = Field(default=None, **_names("mealName", "slotName"))
    menu_meal_name: str | None = Field(default=None, **_names("menuMealName", "dishName"))
    diet_calories_meal_id: int | None = Field(
        default=None, **_names("dietCaloriesMealId", "mealId")
    )
    can_be_changed: bool | None = Field(
        default=None, **_names("canBeChanged", "canEdit", "editable")
    )
    meal_recommended: bool | None = Field(default=None, **_names("mealRecommended", "recommended"))
    review_summary: Any | None = Field(default=None, **_names("reviewSummary"))
    allergens: list[Any] = Field(default_factory=list)
    allergens_with_excluded: list[Any] = Field(
        default_factory=list, **_names("allergensWithExcluded")
    )
    ingredients: list[dict[str, Any]] = Field(default_factory=list)
    nutrition: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_menu_meal_details(cls, data: Any) -> Any:
        """Merge ``menuMealDetails`` into the top level (top level wins)."""
        if not isinstance(data, dict):
            return data
        details = data.get("menuMealDetails")
        if not isinstance(details, dict):
            return data
        merged = {**details, **{k: v for k, v in data.items() if k != "menuMealDetails"}}
        merged["menuMealDetails"] = details
        return merged

    @property
    def allergens_display(self) -> str:
        """Comma-separated allergen labels (raw API values, unchanged)."""
        labels: list[str] = []
        for entry in self.allergens or []:
            if isinstance(entry, str):
                labels.append(entry)
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("allergenName") or entry.get("code")
                if name:
                    labels.append(str(name))
        return ", ".join(labels)


class SwitchResult(ApiModel):
    """Outcome of a (possibly dry-run) meal switch.

    ``response`` holds the raw PUT payload; the remaining non-API fields are
    context added by :meth:`dietly_mcp.client.DietlyClient.switch_meal`.
    """

    order_id: int | None = Field(default=None, **_names("orderId"))
    delivery_id: int | None = Field(default=None, **_names("deliveryId"))
    delivery_meal_id: int | None = Field(default=None, **_names("deliveryMealId", "id"))
    diet_calories_meal_id: int | None = Field(default=None, **_names("dietCaloriesMealId"))
    amount: int | None = None
    seen: str | None = None
    applied: bool = False
    previous_diet_calories_meal_id: int | None = Field(
        default=None, **_names("previousDietCaloriesMealId")
    )
    previous_menu_meal_name: str | None = Field(default=None, **_names("previousMenuMealName"))
    target_menu_meal_name: str | None = Field(default=None, **_names("targetMenuMealName"))
    target_diet_option_name: str | None = Field(default=None, **_names("targetDietOptionName"))
    date: str | None = None
    slot: str | None = None
    response: dict[str, Any] | None = None
