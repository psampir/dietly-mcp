"""Tests for :mod:`dietly_mcp.mcp_server` (read-only MCP tools).

Everything is mocked: the payload builders run against a fake client and the
registered tools are exercised once over the SDK's in-memory transport. No test
touches the network and none performs a write.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from dietly_mcp import mcp_server
from dietly_mcp.config import Settings
from dietly_mcp.errors import (
    CateringMismatchError,
    CsrfTokenError,
    SessionExpiredError,
)
from dietly_mcp.models import DayMeal, MealOption, Order, OrderDetail

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=10)).isoformat()
FUTURE_2 = (TODAY + timedelta(days=12)).isoformat()
PAST = (TODAY - timedelta(days=10)).isoformat()
START = (TODAY - timedelta(days=5)).isoformat()
END = (TODAY + timedelta(days=30)).isoformat()

ORDER_ID = 3597452
HISTORICAL_ID = 152698
DELIVERY_ID = 987654
DELIVERY_ID_2 = 987655
SLOT_BREAKFAST = 555001
SLOT_LUNCH = 555003

ORDER_ROW = {
    "orderId": ORDER_ID,
    "status": "ACTIVE",
    "dateFrom": START,
    "dateTo": END,
    "companyName": "kuchniavikinga",
    "companyDisplayName": "Kuchnia Vikinga",
    "dietName": "Wybór menu",
    "dietCalories": 2000,
    "mealCount": 5,
}

HISTORICAL_ROW = {
    "orderId": HISTORICAL_ID,
    "status": "ACTIVE",  # Dietly stamps _finished_ orders as ACTIVE
    "dateFrom": (TODAY - timedelta(days=60)).isoformat(),
    "dateTo": PAST,
    "companyName": "wybormenu",
    "dietName": "OPTIMAL",
    "dietCalories": 3000,
}

ORDER_DETAIL = {
    "orderId": ORDER_ID,
    "companyName": "kuchniavikinga",
    "deliveries": [
        {"deliveryId": DELIVERY_ID_2, "date": FUTURE_2, "deliveryMeals": []},
        {"deliveryId": DELIVERY_ID, "date": FUTURE, "deliveryMeals": []},
        {"deliveryId": 111, "date": PAST, "deliveryMeals": []},
    ],
}

DAY_MENU = [
    {
        "mealName": "Śniadanie",
        "menuMealName": "Pasta jajeczna",
        "deliveryMealId": SLOT_BREAKFAST,
        "dietCaloriesMealId": 2345,
        "switchable": True,
        "allergens": [{"name": "jajka"}],
        "nutrition": {"calories": 420},
    },
    {
        "mealName": "Obiad",
        "menuMealName": "Kurczak pieczony z kaszą",
        "deliveryMealId": SLOT_LUNCH,
        "dietCaloriesMealId": 2400,
        "switchable": True,
        "allergens": [],
        "nutrition": {"calories": 700},
    },
]

SWITCH_OPTIONS = [
    {"dietOptionName": "SPORT", "menuMealDetails": {
        "menuMealName": "Sałatka z kurczakiem", "dietCaloriesMealId": 2400,
        "mealName": "Obiad", "allergens": [{"name": "seler"}], "nutrition": {"calories": 480}}},
    {"dietOptionName": "OPTIMAL", "mealRecommended": True, "menuMealDetails": {
        "menuMealName": "Łosoś z ryżem", "dietCaloriesMealId": 2401,
        "mealName": "Obiad", "allergens": [], "nutrition": {"calories": 520}}},
    {"dietOptionName": "OPTIMAL", "menuMealDetails": {
        "menuMealName": "Pierogi ruskie", "dietCaloriesMealId": 2402,
        "mealName": "Obiad", "allergens": [], "nutrition": {"calories": 600}}},
]


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeSession:
    """Minimal stand-in for :class:`dietly_mcp.session.Session`."""

    def __init__(self, *, email: str = "user@example.com", valid: bool = True) -> None:
        self.email = email
        self.valid = valid
        self.closed = False

    def is_valid(self) -> bool:
        return self.valid

    def verify(self) -> dict[str, Any]:
        if not self.valid:
            raise SessionExpiredError("expired")
        return {"email": self.email}

    def close(self) -> None:
        self.closed = True


class FakeClient:
    """Read-only fake of :class:`dietly_mcp.client.DietlyClient`."""

    def __init__(
        self,
        *,
        orders: list[dict[str, Any]] | None = None,
        detail: dict[str, Any] | None = None,
        menus: dict[int, list[dict[str, Any]]] | None = None,
        options: dict[int, list[dict[str, Any]]] | None = None,
        valid: bool = True,
        cutoff_hours: float = 36.0,
    ) -> None:
        self.session = FakeSession(valid=valid)
        self.settings = Settings(
            session_path=Path("/tmp/dietly-test/session.json"), edit_cutoff_hours=cutoff_hours
        )
        self._orders = orders if orders is not None else [ORDER_ROW]
        self._detail = detail if detail is not None else ORDER_DETAIL
        self._menus = menus if menus is not None else {DELIVERY_ID: DAY_MENU}
        self._options = options if options is not None else {SLOT_LUNCH: SWITCH_OPTIONS}
        self.read_calls: list[tuple[Any, ...]] = []

    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        self.read_calls.append(("get_orders", active_only, refresh))
        return [Order.model_validate(row) for row in self._orders]

    def get_order(self, order_id: int) -> OrderDetail:
        self.read_calls.append(("get_order", order_id))
        return OrderDetail.model_validate(self._detail)

    def get_day_menu(self, order_id: int, delivery_id: int) -> list[DayMeal]:
        self.read_calls.append(("get_day_menu", order_id, delivery_id))
        return [DayMeal.model_validate(row) for row in self._menus[delivery_id]]

    def get_switch_options(
        self, order_id: int, delivery_id: int, delivery_meal_id: int
    ) -> list[MealOption]:
        self.read_calls.append(("get_switch_options", order_id, delivery_id, delivery_meal_id))
        return [MealOption.model_validate(row) for row in self._options[delivery_meal_id]]


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture(autouse=True)
def _no_cached_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the module-level client cache (or a real connect) leak between tests."""
    monkeypatch.setattr(mcp_server, "_CLIENT", None)
    monkeypatch.setattr(mcp_server, "_connect", lambda: FakeClient())


# --------------------------------------------------------------------------- #
# status_payload
# --------------------------------------------------------------------------- #
def test_status_payload_reports_session_and_current_orders(fake: FakeClient) -> None:
    result = mcp_server.status_payload(fake)

    assert result["ok"] is True
    assert result["session"]["valid"] is True
    assert result["session"]["email"] == "user@example.com"
    assert [o["orderId"] for o in result["activeOrders"]] == [ORDER_ID]
    assert "message" not in result


def test_status_payload_flags_an_expired_session_with_a_hint() -> None:
    result = mcp_server.status_payload(FakeClient(valid=False))

    assert result["ok"] is True
    assert result["session"]["valid"] is False
    assert result["activeOrders"] == []
    assert "dietly-mcp login" in result["message"]


# --------------------------------------------------------------------------- #
# orders_payload
# --------------------------------------------------------------------------- #
def test_orders_payload_filters_history_and_reports_counts() -> None:
    fake = FakeClient(orders=[ORDER_ROW, HISTORICAL_ROW])

    result = mcp_server.orders_payload(fake)

    assert result["ok"] is True
    assert result["count"] == 1
    order = result["orders"][0]
    assert order["orderId"] == ORDER_ID
    assert order["catering"] == "kuchniavikinga"
    assert order["diet"] == "Wybór menu"
    assert order["kcal"] == 2000
    assert order["dateFrom"] == START
    assert order["dateTo"] == END
    assert order["deliveriesCount"] == 3
    # The historical order is never even looked up.
    assert ("get_order", HISTORICAL_ID) not in fake.read_calls


# --------------------------------------------------------------------------- #
# deliveries_payload
# --------------------------------------------------------------------------- #
def test_deliveries_payload_is_sorted_filtered_and_heuristic(fake: FakeClient) -> None:
    result = mcp_server.deliveries_payload(fake)

    assert result["ok"] is True
    assert result["order"]["orderId"] == ORDER_ID
    assert [d["date"] for d in result["days"]] == [FUTURE, FUTURE_2]
    assert all(d["editable"] is True and d["editableIsHeuristic"] is True for d in result["days"])
    assert result["editableHeuristic"]["cutoffHours"] == 36.0
    assert result["editableHeuristic"]["isHeuristic"] is True


def test_deliveries_payload_flags_near_days_as_locked() -> None:
    """A delivery only hours away is (heuristically) not editable."""
    fake = FakeClient(
        detail={
            "orderId": ORDER_ID,
            "companyName": "kuchniavikinga",
            "deliveries": [
                {"deliveryId": 1, "date": TODAY.isoformat(), "deliveryMeals": []},
                {
                    "deliveryId": 2,
                    "date": (TODAY + timedelta(days=30)).isoformat(),
                    "deliveryMeals": [],
                },
            ],
        }
    )

    result = mcp_server.deliveries_payload(fake)

    by_id = {d["deliveryId"]: d["editable"] for d in result["days"]}
    assert by_id[1] is False
    assert by_id[2] is True


def test_deliveries_payload_ambiguous_orders_lists_them_instead_of_guessing() -> None:
    second = {**ORDER_ROW, "orderId": 777, "dateFrom": (TODAY - timedelta(days=1)).isoformat()}
    fake = FakeClient(orders=[ORDER_ROW, second])

    result = mcp_server.deliveries_payload(fake)

    assert result["ok"] is False
    assert "order_id" in result["error"]
    assert [o["orderId"] for o in result["activeOrders"]] == [ORDER_ID, second["orderId"]]
    assert not any(call[0] == "get_order" for call in fake.read_calls)


def test_deliveries_payload_unknown_order_id_is_reported(fake: FakeClient) -> None:
    result = mcp_server.deliveries_payload(fake, order_id=999)

    assert result["ok"] is False
    assert "999" in result["error"]
    assert result["activeOrders"][0]["orderId"] == ORDER_ID


def test_deliveries_payload_historical_order_id_is_reported_as_inactive() -> None:
    fake = FakeClient(orders=[ORDER_ROW, HISTORICAL_ROW])

    result = mcp_server.deliveries_payload(fake, order_id=HISTORICAL_ID)

    assert result["ok"] is False
    assert "not currently active" in result["error"]


def test_deliveries_payload_no_active_orders(fake: FakeClient) -> None:
    result = mcp_server.deliveries_payload(FakeClient(orders=[HISTORICAL_ROW]))

    assert result["ok"] is False
    assert result["activeOrders"] == []


# --------------------------------------------------------------------------- #
# day_payload
# --------------------------------------------------------------------------- #
def test_day_payload_returns_slots_verbatim(fake: FakeClient) -> None:
    result = mcp_server.day_payload(fake, FUTURE)

    assert result["ok"] is True
    assert result["date"] == FUTURE
    assert result["deliveryId"] == DELIVERY_ID
    assert result["slotCount"] == 2
    first = result["slots"][0]
    assert first["slot"] == "Śniadanie"
    assert first["dish"] == "Pasta jajeczna"
    assert first["kcal"] == 420
    assert first["deliveryMealId"] == SLOT_BREAKFAST
    assert first["dietCaloriesMealId"] == 2345
    assert first["switchable"] is True
    assert first["allergens"] == ["jajka"]


def test_day_payload_accepts_relative_dates() -> None:
    fake = FakeClient(
        detail={
            "orderId": ORDER_ID,
            "companyName": "kuchniavikinga",
            "deliveries": [
                {"deliveryId": DELIVERY_ID, "date": TODAY.isoformat(), "deliveryMeals": []},
            ],
        }
    )

    result = mcp_server.day_payload(fake, "today")

    assert result["ok"] is True
    assert result["date"] == TODAY.isoformat()


def test_day_payload_unknown_date_is_a_clean_error(fake: FakeClient) -> None:
    from dietly_mcp.errors import DietlyError

    with pytest.raises(DietlyError) as excinfo:
        mcp_server.day_payload(fake, "1999-01-01")

    assert "1999-01-01" in str(excinfo.value)


def test_day_payload_rejects_a_bad_date_string(fake: FakeClient) -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        mcp_server.day_payload(fake, "15-06-2026")


# --------------------------------------------------------------------------- #
# options_payload
# --------------------------------------------------------------------------- #
def test_options_payload_marks_current_and_sorts_current_first(fake: FakeClient) -> None:
    result = mcp_server.options_payload(fake, FUTURE, "obiad")

    assert result["ok"] is True
    assert result["slot"] == "Obiad"
    assert result["current"] == {"dish": "Kurczak pieczony z kaszą", "targetId": 2400}
    assert result["count"] == 3
    # Current option first, then the recommended one, then the rest.
    assert [o["targetId"] for o in result["options"]] == [2400, 2401, 2402]
    current = result["options"][0]
    assert current["current"] is True
    assert current["variant"] == "SPORT"
    assert current["allergens"] == ["seler"]
    assert result["options"][1]["recommended"] is True


def test_options_payload_unknown_slot_is_a_clean_error(fake: FakeClient) -> None:
    from dietly_mcp.errors import DietlyError

    with pytest.raises(DietlyError) as excinfo:
        mcp_server.options_payload(fake, FUTURE, "Kolacja")

    assert "Kolacja" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (SessionExpiredError("session gone"), "dietly-mcp login"),
        (CsrfTokenError("csrf"), "CSRF"),
        (CateringMismatchError("wrong catering"), "order"),
    ],
)
def test_failure_maps_errors_to_actionable_hints(exc: Exception, needle: str) -> None:
    result = mcp_server._failure(exc)

    assert result["ok"] is False
    assert result["error"]
    assert needle in result["hint"] or needle in result["error"]


def test_failure_maps_bad_dates() -> None:
    result = mcp_server._failure(ValueError("bad"))

    assert result["ok"] is False
    assert "YYYY-MM-DD" in result["hint"]


# --------------------------------------------------------------------------- #
# Tool wrappers: expiry retry + graceful failure (no traceback)
# --------------------------------------------------------------------------- #
class FlakyClient(FakeClient):
    """A client whose first lookup looks like an expired session."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        self.calls += 1
        if self.calls == 1:
            raise SessionExpiredError("expired")
        return super().get_orders(active_only=active_only, refresh=refresh)


def test_run_reconnects_once_when_the_session_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flaky = FlakyClient()
    monkeypatch.setattr(mcp_server, "_connect", lambda: flaky)
    monkeypatch.setattr(mcp_server, "_CLIENT", None)

    result = mcp_server.dietly_orders()

    assert result["ok"] is True
    assert flaky.calls == 2  # first failed, second succeeded
    assert flaky.session.closed is True


def test_tool_returns_actionable_error_when_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> FakeClient:
        raise SessionExpiredError("expired")

    monkeypatch.setattr(mcp_server, "_client", boom)

    result = mcp_server.dietly_status()

    assert result["ok"] is False
    assert "dietly-mcp login" in result["hint"]


def test_tool_never_returns_a_traceback_for_surprising_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> FakeClient:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mcp_server, "_client", boom)

    result = mcp_server.dietly_status()

    assert result["ok"] is False
    assert "RuntimeError" in result["error"]


# --------------------------------------------------------------------------- #
# End-to-end over the SDK in-memory transport (same process → no network)
# --------------------------------------------------------------------------- #
def test_registered_tools_via_in_memory_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from mcp.client import ClientSession
    from mcp.client._memory import InMemoryTransport

    fake = FakeClient(orders=[ORDER_ROW, HISTORICAL_ROW])
    monkeypatch.setattr(mcp_server, "_CLIENT", fake)

    async def run() -> dict[str, Any]:
        async with (
            InMemoryTransport(mcp_server.server) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
            names = sorted(tool.name for tool in listing.tools)
            status = await session.call_tool("dietly_status", {})
            orders = await session.call_tool("dietly_orders", {})
            deliveries = await session.call_tool("dietly_deliveries", {})
            day = await session.call_tool("dietly_day", {"date": FUTURE})
            options = await session.call_tool(
                "dietly_options", {"date": FUTURE, "slot": "Obiad"}
            )
            return {
                "names": names,
                "status": status,
                "orders": orders,
                "deliveries": deliveries,
                "day": day,
                "options": options,
            }

    out = asyncio.run(run())

    assert out["names"] == [
        "dietly_day",
        "dietly_deliveries",
        "dietly_options",
        "dietly_orders",
        "dietly_status",
    ]
    for result in ("status", "orders", "deliveries", "day", "options"):
        assert out[result].is_error is False, result
    assert '"orderId": 3597452' in out["orders"].content[0].text
    assert "Śniadanie" in out["day"].content[0].text
    # Read-only: only read methods were called, and no write method exists.
    methods = {call[0] for call in fake.read_calls}
    assert methods <= {"get_orders", "get_order", "get_day_menu", "get_switch_options"}
