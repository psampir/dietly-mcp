"""CLI tests for the F1 polish: historical-order filtering, sorted deliveries
with the shared editable heuristic, and ``switch --json``.

Everything is mocked through a recording fake; no test touches the network.
"""

from __future__ import annotations

import json as jsonlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from dietly_mcp import cli
from dietly_mcp.config import Settings
from dietly_mcp.models import DayMeal, MealOption, Order, OrderDetail, SwitchResult

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=10)).isoformat()
FUTURE_2 = (TODAY + timedelta(days=12)).isoformat()
PAST = (TODAY - timedelta(days=10)).isoformat()
NEAR = TODAY.isoformat()
START = (TODAY - timedelta(days=5)).isoformat()
END = (TODAY + timedelta(days=30)).isoformat()

ORDER_ID = 3597452
HISTORICAL_ID = 152698
DELIVERY_ID = 987654
SLOT_ID = 555001

CURRENT_ROW = {
    "orderId": ORDER_ID,
    "status": "ACTIVE",
    "dateFrom": START,
    "dateTo": END,
    "companyName": "kuchniavikinga",
    "dietName": "Wybór menu",
    "dietCalories": 2000,
}
HISTORICAL_ROW = {
    "orderId": HISTORICAL_ID,
    "status": "ACTIVE",
    "dateFrom": (TODAY - timedelta(days=60)).isoformat(),
    "dateTo": PAST,
    "companyName": "wybormenu",
    "dietName": "OPTIMAL",
    "dietCalories": 3000,
}

DAY_MENU = [
    {
        "mealName": "Obiad",
        "menuMealName": "Kurczak pieczony z kaszą",
        "deliveryMealId": SLOT_ID,
        "dietCaloriesMealId": 2400,
        "switchable": True,
        "allergens": [],
    }
]

SWITCH_OPTIONS = [
    {
        "dietOptionName": "SPORT",
        "menuMealDetails": {
            "mealName": "Obiad",
            "menuMealName": "Sałatka z kurczakiem",
            "dietCaloriesMealId": 2400,
        },
    },
    {
        "dietOptionName": "OPTIMAL",
        "menuMealDetails": {
            "mealName": "Obiad",
            "menuMealName": "Łosoś z ryżem",
            "dietCaloriesMealId": 2401,
        },
    },
]


class CliFake:
    """Recording stand-in for ``DietlyClient`` covering read + guarded switch."""

    def __init__(
        self,
        *,
        orders: list[dict[str, Any]],
        deliveries: list[dict[str, Any]],
        editable: bool | None = None,
    ) -> None:
        self._orders = orders
        self._deliveries = deliveries
        self._editable = editable
        self.switch_calls: list[dict[str, Any]] = []

    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        return [Order.model_validate(row) for row in self._orders]

    def get_order(self, order_id: int) -> OrderDetail:
        return OrderDetail.model_validate(
            {
                "orderId": order_id,
                "companyName": "kuchniavikinga",
                "deliveries": [
                    {**delivery, "editable": self._editable} for delivery in self._deliveries
                ],
            }
        )

    def get_day_menu(self, order_id: int, delivery_id: int) -> list[DayMeal]:
        return [DayMeal.model_validate(row) for row in DAY_MENU]

    def get_switch_options(
        self, order_id: int, delivery_id: int, delivery_meal_id: int
    ) -> list[MealOption]:
        return [MealOption.model_validate(row) for row in SWITCH_OPTIONS]

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
        self.switch_calls.append({"target_id": target_id, "dry_run": dry_run, "amount": amount})
        return SwitchResult(
            orderId=order_id,
            deliveryId=delivery_id,
            deliveryMealId=delivery_meal_id,
            dietCaloriesMealId=target_id,
            previousDietCaloriesMealId=2400,
            previousMenuMealName="Kurczak pieczony z kaszą",
            targetMenuMealName="Łosoś z ryżem",
            targetDietOptionName="OPTIMAL",
            slot="Obiad",
            applied=not dry_run,
        )

    def switch_meal_url(self, *args: Any, **kwargs: Any) -> str:
        return f"https://panel.dietly.pl/switch/{args[0]}"


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Return a factory that installs a ``CliFake`` into the CLI.

    The default settings keep the audit log under ``tmp_path``, so write
    commands never touch the real state directory.
    """

    def _install(fake: CliFake, settings: Settings | None = None) -> CliFake:
        resolved = settings or Settings(audit_path=tmp_path / "audit.jsonl")
        monkeypatch.setattr(cli, "_client", lambda: (fake, resolved))
        return fake

    return _install


# --------------------------------------------------------------------------- #
# orders: historical filtering + --all opt-out
# --------------------------------------------------------------------------- #
def test_orders_hides_historical_orders_by_default(
    install, capsys: pytest.CaptureFixture[str]
) -> None:
    install(CliFake(orders=[CURRENT_ROW, HISTORICAL_ROW], deliveries=[]))

    assert cli.run_cli(["orders"]) == 0
    out = capsys.readouterr().out
    assert str(ORDER_ID) in out
    assert str(HISTORICAL_ID) not in out
    assert "historical order(s) hidden" in out


def test_orders_all_opt_out_lists_everything(
    install, capsys: pytest.CaptureFixture[str]
) -> None:
    install(CliFake(orders=[CURRENT_ROW, HISTORICAL_ROW], deliveries=[]))

    assert cli.run_cli(["orders", "--all"]) == 0
    out = capsys.readouterr().out
    assert str(ORDER_ID) in out
    assert str(HISTORICAL_ID) in out
    assert "hidden" not in out


def test_orders_json_respects_the_date_filter(
    install, capsys: pytest.CaptureFixture[str]
) -> None:
    install(CliFake(orders=[CURRENT_ROW, HISTORICAL_ROW], deliveries=[]))

    cli.run_cli(["orders", "--json"])
    assert [row["orderId"] for row in jsonlib.loads(capsys.readouterr().out)] == [ORDER_ID]

    cli.run_cli(["orders", "--json", "--all"])
    assert [row["orderId"] for row in jsonlib.loads(capsys.readouterr().out)] == [
        ORDER_ID,
        HISTORICAL_ID,
    ]


# --------------------------------------------------------------------------- #
# deliveries: sorted + editable heuristic
# --------------------------------------------------------------------------- #
def test_deliveries_sorts_days_and_estimates_editability(
    install, capsys: pytest.CaptureFixture[str]
) -> None:
    deliveries = [
        {"deliveryId": 2, "date": FUTURE_2, "deliveryMeals": []},
        {"deliveryId": 1, "date": FUTURE, "deliveryMeals": []},
    ]
    install(CliFake(orders=[CURRENT_ROW], deliveries=deliveries, editable=None))

    assert cli.run_cli(["deliveries"]) == 0
    out = capsys.readouterr().out
    # Rows are printed in date order, not input order.
    assert out.index(FUTURE) < out.index(FUTURE_2)
    assert "estimated" in out
    assert "DIETLY_EDIT_CUTOFF_HOURS" in out


def test_deliveries_json_marks_the_heuristic(install, capsys: pytest.CaptureFixture[str]) -> None:
    deliveries = [
        {"deliveryId": 1, "date": NEAR, "deliveryMeals": []},
        {"deliveryId": 2, "date": FUTURE_2, "deliveryMeals": []},
    ]
    install(CliFake(orders=[CURRENT_ROW], deliveries=deliveries, editable=None))

    assert cli.run_cli(["deliveries", "--json"]) == 0
    payload = jsonlib.loads(capsys.readouterr().out)

    assert [row["date"] for row in payload] == [NEAR, FUTURE_2]
    assert payload[0]["editable"] is False  # today is inside the cutoff
    assert payload[1]["editable"] is True
    assert all(row["editableIsHeuristic"] is True for row in payload)


def test_deliveries_respects_the_api_editable_flag(
    install, capsys: pytest.CaptureFixture[str]
) -> None:
    deliveries = [{"deliveryId": 1, "date": NEAR, "deliveryMeals": []}]
    install(CliFake(orders=[CURRENT_ROW], deliveries=deliveries, editable=True))

    cli.run_cli(["deliveries", "--json"])
    payload = jsonlib.loads(capsys.readouterr().out)

    assert payload[0]["editable"] is True
    assert payload[0]["editableIsHeuristic"] is False


# --------------------------------------------------------------------------- #
# switch --json
# --------------------------------------------------------------------------- #
def _delivery_row() -> dict[str, Any]:
    return {"deliveryId": DELIVERY_ID, "date": FUTURE, "deliveryMeals": []}


def test_switch_json_dry_run(install, capsys: pytest.CaptureFixture[str]) -> None:
    fake = install(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery_row()]))

    result = cli.run_cli(
        ["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "2401", "--json"]
    )
    assert result == 0
    payload = jsonlib.loads(capsys.readouterr().out)

    assert payload["dryRun"] is True
    assert payload["applied"] is False
    assert payload["to"]["dietCaloriesMealId"] == 2401
    assert payload["from"]["dietCaloriesMealId"] == 2400
    assert payload["request"]["method"] == "PUT"
    assert fake.switch_calls[0]["dry_run"] is True


def test_switch_json_apply(install, capsys: pytest.CaptureFixture[str]) -> None:
    fake = install(CliFake(orders=[CURRENT_ROW], deliveries=[_delivery_row()]))

    assert (
        cli.run_cli(
            ["switch", "--date", FUTURE, "--slot", "Obiad", "--to", "2401", "--apply", "--json"]
        )
        == 0
    )
    payload = jsonlib.loads(capsys.readouterr().out)

    assert payload["applied"] is True
    assert payload["dietCaloriesMealId"] == 2401
    assert payload["date"] == FUTURE
    assert fake.switch_calls[0]["dry_run"] is False


def test_switch_help_mentions_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run_cli(["switch", "--help"]) == 0
    assert "--json" in capsys.readouterr().out
