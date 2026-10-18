"""Tests for :mod:`dietly_mcp.logic` — pure date/selection policy.

No network, no clock dependency: every function that reads the current time or
date takes an injectable ``now``/``today``.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from dietly_mcp.logic import (
    allergen_labels,
    coerce_date,
    current_orders,
    is_editable,
    is_order_current,
    nutrition_kcal,
    nutrition_summary,
    parse_date,
    sort_deliveries,
    sort_orders,
    upcoming_deliveries,
)
from dietly_mcp.models import DayMeal, Delivery, MealOption, Order

#: The full ``nutrition`` dict Dietly returns per portion (shape observed live).
FULL_NUTRITION = {
    "weight": 535.0,
    "calories": 427.0,
    "fat": 15.48,
    "protein": 38.1,
    "carbohydrate": 33.44,
    "dietaryFiber": 6.31,
    "sugar": 3.13,
    "salt": 1.21,
    "saturatedFattyAcids": 2.25,
    "caloriesText": "427 kcal / 1788 kJ",
}

TODAY = date(2026, 10, 16)


# --------------------------------------------------------------------------- #
# parse_date / coerce_date
# --------------------------------------------------------------------------- #
def test_parse_date_accepts_iso_and_relative_words() -> None:
    assert parse_date("2026-06-15") == "2026-06-15"
    assert parse_date("today", today=TODAY) == "2026-10-16"
    assert parse_date("  TOMORROW ", today=TODAY) == "2026-10-17"


def test_parse_date_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        parse_date("15-06-2026")
    with pytest.raises(ValueError, match="Invalid isoformat"):
        parse_date("")


def test_coerce_date_truncates_timestamps_and_rejects_nonsense() -> None:
    assert coerce_date("2026-06-15T00:00:00Z") == date(2026, 6, 15)
    assert coerce_date(datetime(2026, 6, 15, 12, 30)) == date(2026, 6, 15)
    assert coerce_date("2026-06-15") == date(2026, 6, 15)
    assert coerce_date(None) is None
    assert coerce_date("") is None
    assert coerce_date("not-a-date") is None


# --------------------------------------------------------------------------- #
# is_editable (heuristic)
# --------------------------------------------------------------------------- #
def test_is_editable_uses_the_36_hour_cutoff() -> None:
    now = datetime(2026, 10, 16, 12, 0)

    # Exactly 36h ahead → editable (midnight of the delivery day).
    assert is_editable("2026-10-18", now=now) is True
    # 12h ahead → locked.
    assert is_editable("2026-10-17", now=now) is False
    # In the past → locked.
    assert is_editable("2026-10-16", now=now) is False
    # Far out → editable.
    assert is_editable("2026-10-26", now=now) is True


def test_is_editable_honours_a_custom_cutoff() -> None:
    now = datetime(2026, 10, 16, 12, 0)

    assert is_editable("2026-10-17", now=now, cutoff_hours=6) is True
    assert is_editable("2026-10-18", now=now, cutoff_hours=72) is False


def test_is_editable_is_false_for_missing_dates() -> None:
    assert is_editable(None) is False
    assert is_editable("") is False


# --------------------------------------------------------------------------- #
# Order activity (the "ACTIVE but historical" quirk)
# --------------------------------------------------------------------------- #
def _order(**kwargs: object) -> Order:
    return Order.model_validate(kwargs)


def test_is_order_current_requires_active_status_and_future_end() -> None:
    assert is_order_current(_order(status="ACTIVE", dateTo="2026-12-10"), today=TODAY) is True
    assert is_order_current(_order(status="ACTIVE", dateTo="2026-08-03"), today=TODAY) is False
    assert is_order_current(_order(status="ACTIVE", dateTo="2026-10-16"), today=TODAY) is True
    assert is_order_current(_order(status="FINISHED", dateTo="2026-12-10"), today=TODAY) is False
    # No end date known → trust the status.
    assert is_order_current(_order(status="ACTIVE"), today=TODAY) is True


def test_current_orders_filters_history_and_sorts_by_start() -> None:
    orders = [
        _order(orderId=2, status="ACTIVE", dateFrom="2026-11-01", dateTo="2026-12-31"),
        _order(orderId=1, status="ACTIVE", dateFrom="2026-01-01", dateTo="2026-02-01"),
        _order(orderId=3, status="ACTIVE", dateFrom="2026-09-01", dateTo="2026-12-10"),
    ]

    result = current_orders(orders, today=TODAY)

    assert [o.order_id for o in result] == [3, 2]


def test_sort_orders_puts_undated_last() -> None:
    orders = [
        _order(orderId=1, status="ACTIVE"),
        _order(orderId=2, status="ACTIVE", dateFrom="2026-01-01"),
    ]
    assert [o.order_id for o in sort_orders(orders)] == [2, 1]


# --------------------------------------------------------------------------- #
# Deliveries
# --------------------------------------------------------------------------- #
def _delivery(day: str | None, delivery_id: int) -> Delivery:
    return Delivery.model_validate({"deliveryId": delivery_id, "date": day})


def test_sort_deliveries_orders_by_date() -> None:
    deliveries = [
        _delivery("2026-10-20", 3),
        _delivery("2026-10-18", 1),
        _delivery("2026-10-22", 4),
        _delivery("2026-10-19", 2),
    ]
    assert [d.delivery_id for d in sort_deliveries(deliveries)] == [1, 2, 3, 4]


def test_upcoming_deliveries_filters_past_and_sorts() -> None:
    deliveries = [
        _delivery("2026-10-26", 3),
        _delivery("2026-10-15", 1),
        _delivery("2026-10-16", 2),  # today counts as upcoming
    ]
    assert [d.delivery_id for d in upcoming_deliveries(deliveries, today=TODAY)] == [2, 3]


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #
def test_allergen_labels_reads_strings_and_dicts() -> None:
    entries = ["gluten", {"name": "jajka"}, {"allergenName": "seler"}, {"code": "X"}]
    assert allergen_labels(entries) == ["gluten", "jajka", "seler", "X"]
    assert allergen_labels([]) == []
    assert allergen_labels(None) == []
    # Blank entries (seen live as [""]) are dropped, not surfaced as noise.
    assert allergen_labels(["", "  ", {"name": ""}, {"code": None}]) == []


def test_nutrition_kcal_reads_common_keys() -> None:
    meal = DayMeal.model_validate({"mealName": "Obiad", "nutrition": {"calories": 420}})
    assert nutrition_kcal(meal) == 420

    option = MealOption.model_validate({"nutrition": {"kcal": "380"}})
    assert nutrition_kcal(option) == 380.0

    assert nutrition_kcal(DayMeal.model_validate({"mealName": "Obiad"})) is None


def test_nutrition_summary_maps_the_full_field_set() -> None:
    meal = DayMeal.model_validate({"mealName": "Obiad", "nutrition": FULL_NUTRITION})

    assert nutrition_summary(meal) == {
        "kcal": 427,
        "protein": 38.1,
        "carbs": 33.4,  # from "carbohydrate"
        "fat": 15.5,
        "fiber": 6.3,  # from "dietaryFiber"
        "sugar": 3.1,
        "salt": 1.2,
        "saturates": 2.2,  # from "saturatedFattyAcids"
        "weight": 535,
        "kcalText": "427 kcal / 1788 kJ",
    }


def test_nutrition_summary_omits_missing_keys() -> None:
    meal = MealOption.model_validate(
        {"nutrition": {"calories": 100, "protein": "5.0", "carbohydrate": "12.3"}}
    )

    assert nutrition_summary(meal) == {"kcal": 100, "protein": 5.0, "carbs": 12.3}


def test_nutrition_summary_is_empty_without_nutrition() -> None:
    assert nutrition_summary(DayMeal.model_validate({"mealName": "Obiad"})) == {}
    assert nutrition_summary(DayMeal.model_validate({"nutrition": None})) == {}
    assert nutrition_summary(object()) == {}  # no ``nutrition`` attribute at all


def test_allergen_validation_strips_blank_entries() -> None:
    meal = DayMeal.model_validate(
        {
            "mealName": "Obiad",
            "allergens": ["", "  ", "gluten", {"name": ""}, {"code": None}],
            "allergensWithExcluded": [""],
        }
    )
    assert meal.allergens == ["gluten"]
    assert meal.allergens_with_excluded == []

    option = MealOption.model_validate(
        {
            "menuMealDetails": {
                "allergens": [""],
                "allergensWithExcluded": [{"name": "seler"}],
            }
        }
    )
    assert option.allergens == []
    assert option.allergens_with_excluded == [{"name": "seler"}]
