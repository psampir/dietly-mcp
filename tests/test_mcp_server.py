"""Tests for :mod:`dietly_mcp.mcp_server` (read-only MCP tools).

Everything is mocked: the payload builders run against a fake client and the
registered tools are exercised once over the SDK's in-memory transport. No test
touches the network and none performs a write.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from dietly_mcp import mcp_server, operations
from dietly_mcp.audit import AuditLog
from dietly_mcp.config import Settings
from dietly_mcp.errors import (
    ApiError,
    CateringMismatchError,
    CsrfTokenError,
    DietlyError,
    SessionExpiredError,
    UnknownMealOptionError,
)
from dietly_mcp.models import DayMeal, MealOption, Order, OrderDetail, SwitchResult

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=10)).isoformat()
FUTURE_2 = (TODAY + timedelta(days=12)).isoformat()
PAST = (TODAY - timedelta(days=10)).isoformat()
START = (TODAY - timedelta(days=5)).isoformat()
END = (TODAY + timedelta(days=30)).isoformat()

ORDER_ID = 1234567
HISTORICAL_ID = 7654321
DELIVERY_ID = 987654
DELIVERY_ID_2 = 987655
SLOT_BREAKFAST = 555001
SLOT_LUNCH = 555003

ORDER_ROW = {
    "orderId": ORDER_ID,
    "status": "ACTIVE",
    "dateFrom": START,
    "dateTo": END,
    "companyName": "example-catering",
    "companyDisplayName": "Example Catering",
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
    "companyName": "example-catering",
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
    """Fake of :class:`dietly_mcp.client.DietlyClient` (reads + guarded switch)."""

    def __init__(
        self,
        *,
        orders: list[dict[str, Any]] | None = None,
        detail: dict[str, Any] | None = None,
        menus: dict[int, list[dict[str, Any]]] | None = None,
        options: dict[int, list[dict[str, Any]]] | None = None,
        valid: bool = True,
        cutoff_hours: float = 36.0,
        audit_path: Path | None = None,
    ) -> None:
        self.session = FakeSession(valid=valid)
        self.settings = Settings(
            session_path=Path("/tmp/dietly-test/session.json"),
            audit_path=audit_path or Path("/tmp/dietly-test/audit.jsonl"),
            edit_cutoff_hours=cutoff_hours,
        )
        self._orders = orders if orders is not None else [ORDER_ROW]
        self._detail = detail if detail is not None else ORDER_DETAIL
        # Deep-copied (per delivery) so a switch can mutate "the current menu"
        # without leaking into other tests or into the other day.
        self._menus = (
            {key: copy.deepcopy(value) for key, value in menus.items()}
            if menus is not None
            else {DELIVERY_ID: copy.deepcopy(DAY_MENU)}
        )
        self._options = options if options is not None else {SLOT_LUNCH: SWITCH_OPTIONS}
        self.read_calls: list[tuple[Any, ...]] = []
        #: Every switch that was actually applied (dry runs are NOT recorded).
        self.applied: list[dict[str, Any]] = []

    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        self.read_calls.append(("get_orders", active_only, refresh))
        if active_only:
            return [
                Order.model_validate(row)
                for row in self._orders
                if row.get("status") == "ACTIVE"
            ]
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

    def switch_meal_url(
        self,
        order_id: int,
        delivery_id: int,
        delivery_meal_id: int,
        target_id: int,
        amount: int = 1,
    ) -> str:
        return (
            f"https://panel.dietly.pl/api/company/customer/order/{order_id}"
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
        """Mirror ``DietlyClient.switch_meal``: record applied writes only.

        An applied switch also updates the fake's current menu, so a revert can
        observe the new state the way the real API would report it.
        """
        target = next(
            (
                o
                for o in self._options[delivery_meal_id]
                if o["menuMealDetails"]["dietCaloriesMealId"] == target_id
            ),
            {},
        )
        current = next(
            (m for m in self._menus[delivery_id] if m["deliveryMealId"] == delivery_meal_id),
            {},
        )
        if not dry_run:
            self.applied.append(
                {
                    "order_id": order_id,
                    "delivery_id": delivery_id,
                    "delivery_meal_id": delivery_meal_id,
                    "target_id": target_id,
                    "amount": amount,
                }
            )
            current["dietCaloriesMealId"] = target_id
            if target.get("menuMealDetails", {}).get("menuMealName"):
                current["menuMealName"] = target["menuMealDetails"]["menuMealName"]
        return SwitchResult(
            orderId=order_id,
            deliveryId=delivery_id,
            deliveryMealId=delivery_meal_id,
            dietCaloriesMealId=target_id,
            previousDietCaloriesMealId=current.get("dietCaloriesMealId"),
            previousMenuMealName=current.get("menuMealName"),
            targetMenuMealName=target.get("menuMealDetails", {}).get("menuMealName"),
            targetDietOptionName=target.get("dietOptionName"),
            slot=current.get("mealName"),
            amount=amount,
            applied=not dry_run,
            seen=None if dry_run else "SWITCHED",
        )


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """A throwaway audit file every write test can inspect."""
    return tmp_path / "audit.jsonl"


@pytest.fixture(autouse=True)
def _redirect_audit(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    """Never let a test write to the real audit log."""
    monkeypatch.setattr(
        operations, "default_audit_log", lambda settings=None: AuditLog(audit_path)
    )


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
    assert order["catering"] == "example-catering"
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
            "companyName": "example-catering",
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
    # The compact nutrition summary is attached to every slot.
    assert first["macros"] == {"kcal": 420}


def test_day_payload_accepts_relative_dates() -> None:
    fake = FakeClient(
        detail={
            "orderId": ORDER_ID,
            "companyName": "example-catering",
            "deliveries": [
                {"deliveryId": DELIVERY_ID, "date": TODAY.isoformat(), "deliveryMeals": []},
            ],
        }
    )

    result = mcp_server.day_payload(fake, "today")

    assert result["ok"] is True
    assert result["date"] == TODAY.isoformat()


def test_day_payload_unknown_date_is_a_clean_error(fake: FakeClient) -> None:
    with pytest.raises(DietlyError) as excinfo:
        mcp_server.day_payload(fake, "1999-01-01")

    assert "1999-01-01" in str(excinfo.value)


def test_day_payload_rejects_a_bad_date_string(fake: FakeClient) -> None:
    with pytest.raises(ValueError, match="Invalid isoformat"):
        mcp_server.day_payload(fake, "15-06-2026")


#: The full per-portion ``nutrition`` dict the live API returns (masked sample).
RICH_NUTRITION = {
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

RICH_MACROS = {
    "kcal": 427,
    "protein": 38.1,
    "carbs": 33.4,
    "fat": 15.5,
    "fiber": 6.3,
    "sugar": 3.1,
    "salt": 1.2,
    "saturates": 2.2,
    "weight": 535,
    "kcalText": "427 kcal / 1788 kJ",
}


def test_day_payload_exposes_full_macros_and_strips_blank_allergens() -> None:
    menu = [
        {
            "mealName": "Obiad",
            "menuMealName": "Dieta Light",
            "deliveryMealId": SLOT_LUNCH,
            "dietCaloriesMealId": 2400,
            "switchable": True,
            "allergens": [""],
            "nutrition": RICH_NUTRITION,
        }
    ]
    fake = FakeClient(menus={DELIVERY_ID: menu})

    result = mcp_server.day_payload(fake, FUTURE)

    slot = result["slots"][0]
    assert slot["kcal"] == 427  # the compatibility field stays
    assert slot["macros"] == RICH_MACROS
    assert slot["allergens"] == []  # the live [""] noise is gone


def test_day_payload_has_an_empty_macros_dict_without_nutrition() -> None:
    menu = [
        {
            "mealName": "Obiad",
            "menuMealName": "Bez danych",
            "deliveryMealId": SLOT_LUNCH,
            "dietCaloriesMealId": 2400,
        }
    ]
    fake = FakeClient(menus={DELIVERY_ID: menu})

    slot = mcp_server.day_payload(fake, FUTURE)["slots"][0]

    assert slot["macros"] == {}
    assert "kcal" not in slot


# --------------------------------------------------------------------------- #
# options_payload
# --------------------------------------------------------------------------- #
def test_options_payload_marks_current_and_sorts_current_first(fake: FakeClient) -> None:
    result = mcp_server.options_payload(fake, FUTURE, "obiad")

    assert result["ok"] is True
    assert result["slot"] == "Obiad"
    assert result["current"] == {
        "dish": "Kurczak pieczony z kaszą",
        "targetId": 2400,
        "macros": {"kcal": 480},
    }
    assert result["count"] == 3
    # Current option first, then the recommended one, then the rest.
    assert [o["targetId"] for o in result["options"]] == [2400, 2401, 2402]
    current = result["options"][0]
    assert current["current"] is True
    assert current["variant"] == "SPORT"
    assert current["allergens"] == ["seler"]
    assert current["macros"] == {"kcal": 480}
    assert result["options"][1]["recommended"] is True


def test_options_current_macros_match_the_offered_current_option() -> None:
    """``current.macros`` mirrors the option entry flagged ``current: true``.

    The day-menu nutrition is deliberately different, so a value of ``999`` here
    would prove ``current`` read the wrong source.
    """
    menu = [
        {
            "mealName": "Obiad",
            "menuMealName": "Dieta Light",
            "deliveryMealId": SLOT_LUNCH,
            "dietCaloriesMealId": 2400,
            "switchable": True,
            "nutrition": {"calories": 999},
        }
    ]
    options = [
        {
            "dietOptionName": "LIGHT",
            "menuMealDetails": {
                "mealName": "Obiad",
                "menuMealName": "Dieta Light",
                "dietCaloriesMealId": 2400,
                "allergens": [],
                "nutrition": RICH_NUTRITION,
            },
        }
    ]
    fake = FakeClient(menus={DELIVERY_ID: menu}, options={SLOT_LUNCH: options})

    result = mcp_server.options_payload(fake, FUTURE, "Obiad")

    current_option = result["options"][0]
    assert current_option["current"] is True
    assert result["current"]["macros"] == current_option["macros"]
    assert result["current"]["macros"] == RICH_MACROS


def test_options_payload_exposes_full_macros_for_every_alternative() -> None:
    options = [
        {
            "dietOptionName": "LIGHT",
            "menuMealDetails": {
                "mealName": "Obiad",
                "menuMealName": "Dieta Light",
                "dietCaloriesMealId": 2400,
                "allergens": [""],
                "nutrition": RICH_NUTRITION,
            },
        }
    ]
    fake = FakeClient(options={SLOT_LUNCH: options})

    result = mcp_server.options_payload(fake, FUTURE, "Obiad")

    option = result["options"][0]
    assert option["macros"] == RICH_MACROS
    assert option["macros"]["protein"] == 38.1
    assert option["allergens"] == []
    assert option["current"] is True


def test_options_payload_unknown_slot_is_a_clean_error(fake: FakeClient) -> None:
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
# Write tools: dietly_switch / dietly_apply / dietly_revert
#
# The write tools go through the server's client cache, so every test binds its
# own fake with the `bind` fixture; `fake.applied` is the list of writes that were
# really sent (a dry run appends nothing).
# --------------------------------------------------------------------------- #
@pytest.fixture
def bind(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Bind a fake client to the server cache so the MCP tools use it."""

    def _bind(fake: FakeClient) -> FakeClient:
        monkeypatch.setattr(mcp_server, "_CLIENT", fake)
        return fake

    return _bind


def _audit(audit_path: Path) -> AuditLog:
    return AuditLog(audit_path)


def test_switch_dry_run_previews_and_sends_nothing(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", 2401)

    assert result["ok"] is True
    assert result["dryRun"] is True
    assert result["applied"] is False
    assert result["orderId"] == ORDER_ID
    assert result["catering"] == "example-catering"
    assert result["date"] == FUTURE
    assert result["slot"] == "Obiad"
    assert result["deliveryId"] == DELIVERY_ID
    assert result["deliveryMealId"] == SLOT_LUNCH
    assert result["from"] == {
        "id": 2400,
        "name": "Kurczak pieczony z kaszą",
        "kcal": 700,
        "macros": {"kcal": 700},
    }
    assert result["to"]["id"] == 2401
    assert result["to"]["name"] == "Łosoś z ryżem"
    assert result["to"]["kcal"] == 520
    assert result["to"]["macros"] == {"kcal": 520}
    assert result["to"]["variant"] == "OPTIMAL"
    assert result["request"]["method"] == "PUT"
    assert "2401" in result["request"]["url"]
    assert result["auditId"]
    # A dry run never sends anything.
    assert fake.applied == []

    entries = _audit(audit_path).entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "dry_run"
    assert entry["dry_run"] is True
    assert entry["result"] == "ok"
    assert entry["tool"] == "dietly_switch"
    assert entry["from"]["id"] == 2400
    assert entry["to"]["id"] == 2401
    assert entry["order_id"] == ORDER_ID
    assert entry["catering"] == "example-catering"
    assert entry["date"] == FUTURE
    assert entry["slot"] == "Obiad"
    assert entry["delivery_meal_id"] == SLOT_LUNCH


def test_switch_apply_performs_the_write_and_audits_it(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", 2401, dry_run=False)

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["dryRun"] is False
    assert result["resultSeen"] == "SWITCHED"
    assert result["result"]["dietCaloriesMealId"] == 2401
    assert len(fake.applied) == 1
    assert fake.applied[0]["target_id"] == 2401
    assert fake.applied[0]["delivery_meal_id"] == SLOT_LUNCH

    entry = _audit(audit_path).entries()[-1]
    assert entry["event"] == "switch"
    assert entry["dry_run"] is False
    assert entry["result"] == "ok"
    assert entry["from"]["id"] == 2400
    assert entry["to"]["id"] == 2401
    assert result["auditId"] == entry["id"]


def test_switch_refuses_a_target_that_was_not_offered(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", 9999, dry_run=False)

    assert result["ok"] is False
    assert "9999" in result["error"]
    assert "2400, 2401, 2402" in result["error"]  # the valid ids are listed
    assert "dietly_options" in result["hint"]
    assert result["applied"] is False
    assert fake.applied == []

    entry = _audit(audit_path).entries()[-1]
    assert entry["result"] == "error"
    assert entry["event"] == "switch"  # an attempted apply, not a dry run


def test_switch_accepts_a_string_target_id(bind: Any) -> None:
    bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", "2401")

    assert result["ok"] is True
    assert result["to"]["id"] == 2401
    assert result["dryRun"] is True


def test_switch_rejects_a_non_numeric_target_id(bind: Any) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", "not-an-id")

    assert result["ok"] is False
    assert "not-an-id" in result["error"]
    assert fake.applied == []


def test_switch_reports_an_unknown_slot_without_writing(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Kolacja", 2401, dry_run=False)

    assert result["ok"] is False
    assert "Kolacja" in result["error"]
    assert fake.applied == []
    assert _audit(audit_path).entries()[-1]["result"] == "error"


def test_apply_dry_run_previews_every_entry_and_sends_nothing(
    bind: Any, audit_path: Path
) -> None:
    fake = bind(FakeClient(menus={DELIVERY_ID: DAY_MENU, DELIVERY_ID_2: DAY_MENU}))

    result = mcp_server.dietly_apply(
        [
            {"date": FUTURE, "slot": "Obiad", "to_id": 2401},
            {"date": FUTURE_2, "slot": "Obiad", "to_id": 2402},
        ]
    )

    assert result["ok"] is True
    assert result["dryRun"] is True
    assert result["count"] == 2
    assert result["applied"] == 0
    assert result["failed"] == 0
    assert all(row["ok"] and row["auditId"] for row in result["results"])
    assert [row["to"]["id"] for row in result["results"]] == [2401, 2402]
    # Both sides of every entry carry macros in the preview.
    assert result["results"][0]["from"]["macros"] == {"kcal": 700}
    assert result["results"][0]["to"]["macros"] == {"kcal": 520}
    assert fake.applied == []

    entries = _audit(audit_path).entries()
    assert [e["event"] for e in entries] == ["dry_run", "dry_run"]
    assert [e["to"]["id"] for e in entries] == [2401, 2402]


def test_apply_apply_performs_every_write(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient(menus={DELIVERY_ID: DAY_MENU, DELIVERY_ID_2: DAY_MENU}))

    result = mcp_server.dietly_apply(
        [
            {"date": FUTURE, "slot": "Obiad", "to_id": 2401},
            {"date": FUTURE_2, "slot": "Obiad", "to_id": 2402},
        ],
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["applied"] == 2
    assert result["failed"] == 0
    assert [row["applied"] for row in result["results"]] == [True, True]
    assert [row["target_id"] for row in fake.applied] == [2401, 2402]
    # The result (not just the preview) still shows the macros that were written.
    assert result["results"][0]["from"]["macros"] == {"kcal": 700}
    assert result["results"][0]["to"]["macros"] == {"kcal": 520}
    assert [e["event"] for e in _audit(audit_path).entries()] == ["switch", "switch"]


class FlakySwitchClient(FakeClient):
    """A client whose second *applied* switch fails (the PUT is rejected)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.switch_calls = 0

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
        if not dry_run:
            self.switch_calls += 1
            if self.switch_calls == 2:
                raise ApiError("Dietly returned HTTP 400 for the switch.")
        return super().switch_meal(
            order_id, delivery_id, delivery_meal_id, target_id, amount=amount, dry_run=dry_run
        )


def test_apply_never_aborts_silently_on_a_partial_failure(
    bind: Any, audit_path: Path
) -> None:
    fake = bind(FlakySwitchClient(menus={DELIVERY_ID: DAY_MENU, DELIVERY_ID_2: DAY_MENU}))

    result = mcp_server.dietly_apply(
        [
            {"date": FUTURE, "slot": "Obiad", "to_id": 2401},
            {"date": FUTURE_2, "slot": "Obiad", "to_id": 2402},
        ],
        dry_run=False,
    )

    assert result["ok"] is False  # not everything succeeded...
    assert result["applied"] == 1
    assert result["failed"] == 1
    assert "1 of 2" in result["summary"]
    first, second = result["results"]
    assert first["ok"] is True
    assert second["ok"] is False
    assert "HTTP 400" in second["error"]
    # The first write really happened and was audited before the failure.
    assert [row["target_id"] for row in fake.applied] == [2401]
    assert [e["result"] for e in _audit(audit_path).entries()] == ["ok", "error"]


def test_apply_shape_problems_fail_the_whole_batch(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_apply(
        [
            {"date": FUTURE, "slot": "Obiad"},  # missing to_id
            {"date": "not-a-date", "slot": "Obiad", "to_id": 2401},
            {"date": FUTURE, "slot": "Obiad", "to_id": 2401},
        ],
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["failed"] == 2
    assert [p["index"] for p in result["problems"]] == [0, 1]
    assert "to_id" in result["problems"][0]["error"]
    assert fake.applied == []  # nothing was sent, not even the valid entry
    assert _audit(audit_path).entries()[-1]["result"] == "error"


def test_apply_unoffered_target_fails_the_whole_batch(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_apply(
        [
            {"date": FUTURE, "slot": "Obiad", "to_id": 2401},
            {"date": FUTURE, "slot": "Obiad", "to_id": 9999},
        ],
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["problems"][0]["to_id"] == 9999
    assert "9999" in result["problems"][0]["error"]
    assert fake.applied == []


def test_apply_rejects_an_empty_batch(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_apply([])

    assert result["ok"] is False
    assert "No entries" in result["error"]
    assert fake.applied == []
    assert _audit(audit_path).entries()[-1]["result"] == "error"


def test_revert_restores_the_previous_meal(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())

    applied = mcp_server.dietly_switch(FUTURE, "Obiad", 2401, dry_run=False)
    assert applied["ok"] is True

    result = mcp_server.dietly_revert()

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["revertedAuditId"] == applied["auditId"]
    assert result["from"]["id"] == 2401  # what was undone
    assert result["to"]["id"] == 2400  # the restored meal id
    assert result["revertedSwitch"]["from"]["name"] == "Kurczak pieczony z kaszą"
    assert result["slot"] == "Obiad"
    assert result["dayOpen"]["open"] is True
    # Two writes: the switch, then the revert.
    assert [row["target_id"] for row in fake.applied] == [2401, 2400]

    entries = _audit(audit_path).entries()
    assert [e["event"] for e in entries] == ["switch", "revert"]
    revert_entry = entries[-1]
    assert revert_entry["from"]["id"] == 2401
    assert revert_entry["to"]["id"] == 2400
    assert revert_entry["revertedAuditId"] == applied["auditId"]
    assert revert_entry["tool"] == "dietly_revert"


def test_revert_by_audit_id_and_by_date_filter(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient(menus={DELIVERY_ID: DAY_MENU, DELIVERY_ID_2: DAY_MENU}))

    first = mcp_server.dietly_switch(FUTURE, "Obiad", 2401, dry_run=False)
    second = mcp_server.dietly_switch(FUTURE_2, "Obiad", 2402, dry_run=False)

    # An explicit id wins over "the most recent one".
    reverted = mcp_server.dietly_revert(audit_id=first["auditId"])
    assert reverted["revertedAuditId"] == first["auditId"]
    assert reverted["to"]["id"] == 2400

    # With no id, the date filter selects the remaining applied switch.
    by_date = mcp_server.dietly_revert(date=FUTURE_2)
    assert by_date["revertedAuditId"] == second["auditId"]
    assert by_date["to"]["id"] == 2400
    assert [row["target_id"] for row in fake.applied] == [2401, 2402, 2400, 2400]


def test_revert_without_a_recorded_switch_is_a_clean_error(
    bind: Any, audit_path: Path
) -> None:
    fake = bind(FakeClient())

    result = mcp_server.dietly_revert()

    assert result["ok"] is False
    assert "audit" in result["error"].lower()
    assert fake.applied == []
    assert _audit(audit_path).entries()[-1]["event"] == "revert"


def test_revert_refuses_a_dry_run_entry(bind: Any, audit_path: Path) -> None:
    bind(FakeClient())

    preview = mcp_server.dietly_switch(FUTURE, "Obiad", 2401)

    result = mcp_server.dietly_revert(audit_id=preview["auditId"])

    assert result["ok"] is False
    assert "not an applied switch" in result["error"]


def test_revert_refuses_when_the_previous_meal_is_gone(bind: Any, audit_path: Path) -> None:
    fake = bind(FakeClient())
    # A hand-written record pointing at a meal Dietly no longer offers.
    _audit(audit_path).record(
        event="switch",
        result="ok",
        dry_run=False,
        tool="dietly_switch",
        order_id=ORDER_ID,
        catering="example-catering",
        delivery_id=DELIVERY_ID,
        date=FUTURE,
        slot="Obiad",
        delivery_meal_id=SLOT_LUNCH,
        from_={"id": 9999, "name": "Wycofane danie", "kcal": 1},
        to={"id": 2401, "name": "Łosoś z ryżem", "kcal": 520},
    )

    result = mcp_server.dietly_revert()

    assert result["ok"] is False
    assert "9999" in result["error"]
    assert "2400, 2401, 2402" in result["error"]
    assert fake.applied == []


def test_revert_refuses_when_the_day_is_locked(bind: Any, audit_path: Path) -> None:
    locked = {
        "orderId": ORDER_ID,
        "companyName": "example-catering",
        "deliveries": [
            {"deliveryId": DELIVERY_ID, "date": TODAY.isoformat(), "deliveryMeals": []}
        ],
    }
    fake = bind(FakeClient(detail=locked))
    _audit(audit_path).record(
        event="switch",
        result="ok",
        dry_run=False,
        tool="dietly_switch",
        order_id=ORDER_ID,
        catering="example-catering",
        delivery_id=DELIVERY_ID,
        date=TODAY.isoformat(),
        slot="Obiad",
        delivery_meal_id=SLOT_LUNCH,
        from_={"id": 2400, "name": "Kurczak pieczony z kaszą", "kcal": 700},
        to={"id": 2401, "name": "Łosoś z ryżem", "kcal": 520},
    )

    result = mcp_server.dietly_revert()

    assert result["ok"] is False
    assert "cutoff" in result["error"]
    assert fake.applied == []


def test_switch_auto_selects_the_single_current_order(bind: Any) -> None:
    """Dietly stamps finished orders ACTIVE too; the write path must not choke."""
    bind(FakeClient(orders=[ORDER_ROW, HISTORICAL_ROW]))

    result = mcp_server.dietly_switch(FUTURE, "Obiad", 2401)

    assert result["ok"] is True
    assert result["orderId"] == ORDER_ID


def test_write_path_order_resolution_policy() -> None:
    """Prefer the single *current* order; fall back to the single ACTIVE one."""
    mixed = FakeClient(orders=[ORDER_ROW, HISTORICAL_ROW])
    assert operations._require_order(mixed, None).order_id == ORDER_ID
    # An explicit id of a finished-but-ACTIVE order is still honoured.
    assert operations._require_order(mixed, HISTORICAL_ID).order_id == HISTORICAL_ID

    # Nothing is current, but exactly one order is ACTIVE: use it.
    only_historical = FakeClient(orders=[HISTORICAL_ROW])
    assert operations._require_order(only_historical, None).order_id == HISTORICAL_ID

    # Two genuinely-current orders need an explicit id...
    ambiguous = FakeClient(orders=[ORDER_ROW, {**ORDER_ROW, "orderId": 777}])
    with pytest.raises(DietlyError, match="Several orders"):
        operations._require_order(ambiguous, None)
    # ...an unknown id is reported with the ids we do know...
    with pytest.raises(DietlyError, match="No order with id 999"):
        operations._require_order(mixed, 999)
    # ...and an account with nothing active is a clean error.
    with pytest.raises(DietlyError, match="No active orders"):
        operations._require_order(FakeClient(orders=[]), None)


def test_switch_reports_an_unknown_order_id(bind: Any) -> None:
    bind(FakeClient())

    result = mcp_server.dietly_switch(FUTURE, "Obiad", 2401, order_id=999)

    assert result["ok"] is False
    assert "999" in result["error"]


def test_failure_maps_an_unoffered_target_to_a_hint() -> None:
    result = mcp_server._failure(UnknownMealOptionError("Meal id 1 is not offered."))

    assert result["ok"] is False
    assert "dietly_options" in result["hint"]


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
            switch = await session.call_tool(
                "dietly_switch", {"date": FUTURE, "slot": "Obiad", "to_id": 2401}
            )
            apply_ = await session.call_tool(
                "dietly_apply",
                {"entries": [{"date": FUTURE, "slot": "Obiad", "to_id": 2402}]},
            )
            revert = await session.call_tool("dietly_revert", {})
            return {
                "names": names,
                "status": status,
                "orders": orders,
                "deliveries": deliveries,
                "day": day,
                "options": options,
                "switch": switch,
                "apply": apply_,
                "revert": revert,
            }

    out = asyncio.run(run())

    assert out["names"] == [
        "dietly_apply",
        "dietly_day",
        "dietly_deliveries",
        "dietly_options",
        "dietly_orders",
        "dietly_revert",
        "dietly_status",
        "dietly_switch",
    ]
    for result in ("status", "orders", "deliveries", "day", "options", "switch", "apply", "revert"):
        assert out[result].is_error is False, result
    assert '"orderId": 1234567' in out["orders"].content[0].text
    assert "Śniadanie" in out["day"].content[0].text
    # The write tools were called with their dry_run default: the previews came
    # back, but nothing was ever sent to Dietly.
    assert '"dryRun": true' in out["switch"].content[0].text
    assert '"dryRun": true' in out["apply"].content[0].text
    assert "audit" in out["revert"].content[0].text.lower()
    assert fake.applied == []
