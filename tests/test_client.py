"""Tests for :mod:`dietly_mcp.client` against ``httpx.MockTransport``.

Covers orders pagination, day-menu parsing, switch option parsing, the
dry-run/apply distinction (asserting the exact PUT URL and query) and the
HTTP 490 catering-mismatch mapping.
"""

from __future__ import annotations

import json as jsonlib
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from dietly_mcp import cli
from dietly_mcp.client import DietlyClient, resolve_delivery, resolve_slot
from dietly_mcp.config import Settings
from dietly_mcp.errors import (
    ApiError,
    CateringMismatchError,
    NetworkError,
    NotFoundError,
    UnknownMealOptionError,
)
from dietly_mcp.models import DayMeal, MealOption, Order, OrderDetail, SwitchResult
from dietly_mcp.session import Session

ORDER_ID = 152698
DELIVERY_ID = 987654
SLOT_ID = 555001

ORDERS_PAGE_0 = {
    "content": [
        {
            "orderId": ORDER_ID,
            "status": "ACTIVE",
            "dateFrom": "2026-06-03",
            "dateTo": "2026-08-03",
            "companyName": "wybormenu",
            "dietCalories": 3000,
            "dietName": "OPTIMAL",
        }
    ],
    "totalPages": 2,
    "number": 0,
}

ORDERS_PAGE_1 = {
    "content": [
        {
            "orderId": 111111,
            "status": "FINISHED",
            "dateFrom": "2025-01-01",
            "dateTo": "2025-02-01",
            "companyName": "innykatering",
            "dietCalories": 2000,
            "dietName": "SLIM",
        }
    ],
    "totalPages": 2,
    "number": 1,
}

ORDER_DETAIL = {
    "orderId": ORDER_ID,
    "companyName": "wybormenu",
    "deliveries": [
        {
            "deliveryId": DELIVERY_ID,
            "date": "2026-06-15",
            "deliveryMeals": [{"deliveryMealId": SLOT_ID, "dietCaloriesMealId": 2345}],
        },
        {
            "deliveryId": 987655,
            "date": "2026-06-16",
            "deliveryMeals": [{"deliveryMealId": 555002, "dietCaloriesMealId": 2350}],
        },
    ],
    "nearestDelivery": {"deliveryDates": ["2026-06-15", "2026-06-16"]},
}

DAY_MENU = {
    "deliveryMenuMeal": [
        {
            "mealName": "Śniadanie",
            "menuMealName": "Pasta jajeczna z pieczywem",
            "deliveryMealId": SLOT_ID,
            "dietCaloriesMealId": 2345,
            "switchable": True,
            "allergens": [{"name": "jajka"}],
            "allergensWithExcluded": [],
            "ingredients": [
                {
                    "name": "jajka",
                    "exclusion": [{"dietaryExclusionId": 7, "name": "jajka", "chosen": False}],
                }
            ],
        },
        {
            "mealName": "Obiad",
            "menuMealName": "Kurczak pieczony z kaszą",
            "deliveryMealId": 555003,
            "dietCaloriesMealId": 2400,
            "switchable": False,
            "allergens": [],
            "ingredients": [],
        },
    ]
}

SWITCH_OPTIONS = {
    "mealChangeOptions": [
        {
            "dietOptionName": "SPORT",
            "mealName": "Śniadanie",
            "canBeChanged": True,
            "mealRecommended": False,
            "reviewSummary": None,
            "menuMealDetails": {
                "mealName": "Śniadanie",
                "menuMealName": "Sałatka z kurczakiem",
                "dietCaloriesMealId": 2345,
                "allergens": [{"name": "seler"}],
                "allergensWithExcluded": [],
                "ingredients": [],
                "nutrition": {"calories": 420},
            },
        },
        {
            "dietOptionName": "OPTIMAL",
            "mealName": "Śniadanie",
            "canBeChanged": True,
            "mealRecommended": True,
            "reviewSummary": "Klienci polecają",
            "menuMealDetails": {
                "mealName": "Śniadanie",
                "menuMealName": "Jajecznica z pomidorami",
                "dietCaloriesMealId": 2354,
                "allergens": [],
                "allergensWithExcluded": [{"name": "gluten"}],
                "ingredients": [],
                "nutrition": {"calories": 380},
            },
        },
    ]
}

SWITCH_RESPONSE = {
    "deliveryMealId": SLOT_ID,
    "amount": 1,
    "dietCaloriesMealId": 2354,
    "seen": "SWITCHED",
}


@pytest.fixture
def client(session: Session) -> DietlyClient:
    """A client bound to the mock-transport session."""
    return DietlyClient(session)


def _register_common(
    responses: dict[str, httpx.Response], *, order_detail: dict | None = None
) -> None:
    """Register the routes every client test needs (single orders page)."""
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json={**ORDERS_PAGE_0, "totalPages": 1}
    )
    responses[f"GET /api/company/customer/order/{ORDER_ID}"] = httpx.Response(
        200, json=order_detail or ORDER_DETAIL
    )
    responses[f"GET /api/company/general/menus/delivery/{DELIVERY_ID}/new"] = httpx.Response(
        200, json=DAY_MENU
    )
    responses[
        f"GET /api/company/customer/order/{ORDER_ID}/deliveries/{DELIVERY_ID}"
        f"/delivery-meals/{SLOT_ID}/switch"
    ] = httpx.Response(200, json=SWITCH_OPTIONS)


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def test_get_orders_parses_fields_and_follows_pagination(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/api/profile/profile-order/all"
        page = request.url.params.get("page")
        return httpx.Response(200, json=ORDERS_PAGE_0 if page == "0" else ORDERS_PAGE_1)

    client.session.http = httpx.Client(transport=httpx.MockTransport(handler))

    orders = client.get_orders()

    assert [o.order_id for o in orders] == [ORDER_ID, 111111]
    assert [request.url.params.get("page") for request in seen] == ["0", "1"]
    first = orders[0]
    assert first.status == "ACTIVE"
    assert first.is_active is True
    assert first.company_name == "wybormenu"
    assert first.diet_calories == 3000
    assert first.diet_name == "OPTIMAL"
    assert first.date_from == "2026-06-03"
    assert first.catering_slug == "wybormenu"
    assert first.label == f"{ORDER_ID} · wybormenu · OPTIMAL 3000 kcal"
    assert orders[1].is_active is False


def test_get_orders_active_only_filter(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200,
        json={"content": [*ORDERS_PAGE_0["content"], *ORDERS_PAGE_1["content"]], "totalPages": 1},
    )

    active = client.get_orders(active_only=True)

    assert [o.order_id for o in active] == [ORDER_ID]


def test_get_orders_tolerates_bare_list_payload(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json=[{"orderId": 1, "status": "ACTIVE"}]
    )

    orders = client.get_orders()

    assert len(orders) == 1
    assert orders[0].order_id == 1


def test_get_orders_unexpected_shape_raises_api_error(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json={"unexpected": "payload"}
    )

    with pytest.raises(ApiError) as excinfo:
        client.get_orders()

    assert "no orders list found" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Company id / catering
# --------------------------------------------------------------------------- #
def test_company_id_is_taken_from_the_order_list(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200,
        json={
            "content": [{"orderId": ORDER_ID, "status": "ACTIVE", "companyName": "gymdiet"}],
            "totalPages": 1,
        },
    )

    client.get_order(ORDER_ID)

    detail_request = [r for r in seen if "customer/order" in r.url.path][0]
    assert detail_request.headers["company-id"] == "gymdiet"


def test_company_id_falls_back_to_default_slug(
    settings: Settings,
    session: Session,
    responses: dict[str, httpx.Response],
    seen: list[httpx.Request],
) -> None:
    client = DietlyClient(session, company_id="wybormenu")
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json={"content": [], "totalPages": 1}
    )
    responses[f"GET /api/company/customer/order/{ORDER_ID}"] = httpx.Response(
        200, json=ORDER_DETAIL
    )

    client.get_order(ORDER_ID)

    assert seen[-1].headers["company-id"] == "wybormenu"


def test_490_raises_catering_mismatch(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json={"content": [], "totalPages": 1}
    )
    responses[f"GET /api/company/customer/order/{ORDER_ID}"] = httpx.Response(
        490, json={"message": "Zamówienie 152698 nie istnieje w tym cateringu"}
    )

    with pytest.raises(CateringMismatchError) as excinfo:
        client.get_order(ORDER_ID)

    assert excinfo.value.status_code == 490
    assert "different catering" in str(excinfo.value)


def test_network_failure_is_wrapped(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client.session.http = httpx.Client(transport=httpx.MockTransport(boom))

    with pytest.raises(NetworkError) as excinfo:
        client.get_orders()

    assert "Could not reach" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Day menu
# --------------------------------------------------------------------------- #
def test_get_day_menu_parses_slots_verbatim(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    _register_common(responses)

    meals = client.get_day_menu(ORDER_ID, DELIVERY_ID)

    assert [m.meal_name for m in meals] == ["Śniadanie", "Obiad"]  # Polish, untranslated
    breakfast = meals[0]
    assert breakfast.menu_meal_name == "Pasta jajeczna z pieczywem"
    assert breakfast.delivery_meal_id == SLOT_ID
    assert breakfast.diet_calories_meal_id == 2345
    assert breakfast.switchable is True
    assert breakfast.ingredients[0]["name"] == "jajka"
    assert breakfast.ingredients[0]["exclusion"][0]["dietaryExclusionId"] == 7
    assert meals[1].switchable is False


def test_resolve_slot_is_case_insensitive_but_verbatim(
    client: DietlyClient, responses: dict[str, httpx.Response]
) -> None:
    _register_common(responses)
    meals = client.get_day_menu(ORDER_ID, DELIVERY_ID)

    assert resolve_slot(meals, "obiad").diet_calories_meal_id == 2400
    assert resolve_slot(meals, "  ŚNIADANIE ").delivery_meal_id == SLOT_ID

    with pytest.raises(NotFoundError) as excinfo:
        resolve_slot(meals, "Kolacja")
    assert "Slots: Obiad, Śniadanie" in str(excinfo.value)


def test_resolve_delivery_reports_available_dates() -> None:
    detail = OrderDetail.model_validate(ORDER_DETAIL)

    assert resolve_delivery(detail, "2026-06-16").delivery_id == 987655

    with pytest.raises(NotFoundError) as excinfo:
        resolve_delivery(detail, "2027-01-01")
    assert "2026-06-15, 2026-06-16" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Switch options
# --------------------------------------------------------------------------- #
def test_get_switch_options_parses_nested_menu_meal_details(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)

    options = client.get_switch_options(ORDER_ID, DELIVERY_ID, SLOT_ID)

    assert [o.diet_option_name for o in options] == ["SPORT", "OPTIMAL"]
    first, second = options
    assert first.menu_meal_name == "Sałatka z kurczakiem"
    assert first.diet_calories_meal_id == 2345
    assert first.allergens_display == "seler"
    assert second.meal_recommended is True
    assert second.can_be_changed is True
    assert second.allergens == []
    assert second.nutrition == {"calories": 380}
    request = seen[-1]
    assert request.method == "GET"
    assert request.url.path.endswith(f"/delivery-meals/{SLOT_ID}/switch")
    assert request.url.query == b""


# --------------------------------------------------------------------------- #
# Switch: dry run vs apply
# --------------------------------------------------------------------------- #
def test_switch_dry_run_sends_nothing(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)

    result = client.switch_meal(ORDER_ID, DELIVERY_ID, SLOT_ID, 2354)

    assert result.applied is False
    assert result.delivery_meal_id == SLOT_ID
    assert result.diet_calories_meal_id == 2354
    assert result.previous_diet_calories_meal_id == 2345
    assert result.target_menu_meal_name == "Jajecznica z pomidorami"
    assert result.target_diet_option_name == "OPTIMAL"
    assert [request.method for request in seen] == ["GET", "GET", "GET"]
    assert all("delivery-meals" not in r.url.path or r.method == "GET" for r in seen)


def test_switch_apply_issues_put_with_exact_query(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)
    switch_path = (
        f"/api/company/customer/order/{ORDER_ID}/deliveries/{DELIVERY_ID}"
        f"/delivery-meals/{SLOT_ID}/switch"
    )
    responses[f"PUT {switch_path}"] = httpx.Response(200, json=SWITCH_RESPONSE)

    result = client.switch_meal(ORDER_ID, DELIVERY_ID, SLOT_ID, 2354, dry_run=False)

    puts = [request for request in seen if request.method == "PUT"]
    assert len(puts) == 1
    put = puts[0]
    assert put.url.path == switch_path
    assert put.url.params["amount"] == "1"
    assert put.url.params["dietCaloriesMealId"] == "2354"
    assert put.headers["x-launcher-type"] == "BROWSER_DIETLY"
    assert put.headers["company-id"] == "wybormenu"
    # The PUT must be preceded by the GETs that validated the target id.
    assert [r.method for r in seen][:3] == ["GET", "GET", "GET"]
    assert result.applied is True
    assert result.seen == "SWITCHED"
    assert result.diet_calories_meal_id == 2354
    assert result.previous_diet_calories_meal_id == 2345
    assert result.target_menu_meal_name == "Jajecznica z pomidorami"


def test_switch_honours_amount(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)
    switch_path = (
        f"/api/company/customer/order/{ORDER_ID}/deliveries/{DELIVERY_ID}"
        f"/delivery-meals/{SLOT_ID}/switch"
    )
    responses[f"PUT {switch_path}"] = httpx.Response(200, json=SWITCH_RESPONSE)

    client.switch_meal(ORDER_ID, DELIVERY_ID, SLOT_ID, 2354, amount=2, dry_run=False)

    put = [request for request in seen if request.method == "PUT"][0]
    assert put.url.params["amount"] == "2"


def test_switch_to_unoffered_target_is_refused_before_any_put(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)

    with pytest.raises(UnknownMealOptionError) as excinfo:
        client.switch_meal(ORDER_ID, DELIVERY_ID, SLOT_ID, 9999, dry_run=False)

    message = str(excinfo.value)
    assert "9999" in message
    assert "2354 (OPTIMAL — Jajecznica z pomidorami)" in message
    # Only read calls were made — no PUT was sent.
    assert {request.method for request in seen} == {"GET"}


def test_switch_url_helper_matches_what_is_sent(
    client: DietlyClient, responses: dict[str, httpx.Response], seen: list[httpx.Request]
) -> None:
    _register_common(responses)
    switch_path = (
        f"/api/company/customer/order/{ORDER_ID}/deliveries/{DELIVERY_ID}"
        f"/delivery-meals/{SLOT_ID}/switch"
    )
    responses[f"PUT {switch_path}"] = httpx.Response(200, json=SWITCH_RESPONSE)

    client.switch_meal(ORDER_ID, DELIVERY_ID, SLOT_ID, 2354, dry_run=False)

    put = [request for request in seen if request.method == "PUT"][0]
    assert str(put.url) == client.switch_meal_url(ORDER_ID, DELIVERY_ID, SLOT_ID, 2354)


# --------------------------------------------------------------------------- #
# CLI wiring (smoke tests, no network)
# --------------------------------------------------------------------------- #
ORDER_ROW = {"orderId": ORDER_ID, "status": "ACTIVE", "companyName": "wybormenu"}


class RecordingClient:
    """Minimal stand-in for DietlyClient that records the calls the CLI makes."""

    def __init__(self) -> None:
        self.switch_calls: list[dict[str, Any]] = []

    def get_orders(self, *, active_only: bool = False, refresh: bool = False) -> list[Order]:
        return [Order.model_validate(ORDER_ROW)]

    def get_order(self, order_id: int) -> OrderDetail:
        return OrderDetail.model_validate(ORDER_DETAIL)

    def get_day_menu(self, order_id: int, delivery_id: int) -> list[DayMeal]:
        return [DayMeal.model_validate(meal) for meal in DAY_MENU["deliveryMenuMeal"]]

    def get_switch_options(
        self, order_id: int, delivery_id: int, delivery_meal_id: int
    ) -> list[MealOption]:
        return [MealOption.model_validate(o) for o in SWITCH_OPTIONS["mealChangeOptions"]]

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
        self.switch_calls.append(
            {
                "order_id": order_id,
                "delivery_id": delivery_id,
                "delivery_meal_id": delivery_meal_id,
                "target_id": target_id,
                "amount": amount,
                "dry_run": dry_run,
            }
        )
        return SwitchResult(
            orderId=ORDER_ID,
            deliveryMealId=SLOT_ID,
            dietCaloriesMealId=target_id,
            previousDietCaloriesMealId=2345,
            targetMenuMealName="Jajecznica z pomidorami",
            slot="Śniadanie",
            applied=not dry_run,
        )

    def switch_meal_url(self, *args: Any, **kwargs: Any) -> str:
        return (
            "https://dietly.pl/api/company/customer/order/152698/deliveries/987654"
            f"/delivery-meals/{SLOT_ID}/switch?amount=1&dietCaloriesMealId=2354"
        )


@pytest.fixture
def cli_client(monkeypatch: pytest.MonkeyPatch) -> RecordingClient:
    """Patch the CLI's client factory with a recording fake."""
    fake = RecordingClient()
    monkeypatch.setattr(cli, "_client", lambda: (fake, object()))
    return fake


def test_cli_switch_defaults_to_dry_run(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(
        ["switch", "--date", "2026-06-15", "--slot", "Śniadanie", "--to", "2354"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "DRY RUN" in output
    assert "Nothing was sent" in output or "nothing was sent" in output
    assert "Jajecznica z pomidorami" in output
    assert "--apply" in output
    assert len(cli_client.switch_calls) == 1
    assert cli_client.switch_calls[0]["dry_run"] is True
    assert cli_client.switch_calls[0]["target_id"] == 2354
    assert cli_client.switch_calls[0]["amount"] == 1


def test_cli_switch_apply_performs_the_write(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(
        ["switch", "--date", "2026-06-15", "--slot", "Śniadanie", "--to", "2354", "--apply"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "DRY RUN" not in output
    assert "Switched Śniadanie on 2026-06-15" in output
    assert cli_client.switch_calls[0]["dry_run"] is False


def test_cli_switch_forwards_amount(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.run_cli(
        [
            "switch",
            "--date",
            "2026-06-15",
            "--slot",
            "Śniadanie",
            "--to",
            "2354",
            "--amount",
            "2",
        ]
    )

    assert cli_client.switch_calls[0]["amount"] == 2


def test_cli_day_renders_polish_slot_names(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(["day", "--date", "2026-06-15"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Śniadanie" in output
    assert "Obiad" in output
    assert "Pasta jajeczna z pieczywem" in output


def test_cli_day_json_is_machine_readable(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(["day", "--date", "2026-06-15", "--json"])

    payload = jsonlib.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["orderId"] == ORDER_ID
    assert payload["date"] == "2026-06-15"
    assert [meal["mealName"] for meal in payload["meals"]] == ["Śniadanie", "Obiad"]
    assert payload["meals"][0]["dietCaloriesMealId"] == 2345


def test_cli_options_lists_alternatives(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(["options", "--date", "2026-06-15", "--slot", "śniadanie", "--json"])

    payload = jsonlib.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["currentMealId"] == 2345
    assert [option["dietOptionName"] for option in payload["options"]] == ["SPORT", "OPTIMAL"]
    assert payload["options"][1]["dietCaloriesMealId"] == 2354


def test_cli_unknown_slot_is_a_clean_error(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(["day", "--date", "2026-06-15", "--slot"])

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "Traceback" not in captured.err


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.run_cli(["--help"])

    output = capsys.readouterr().out
    assert exit_code == 0
    for command in ("login", "status", "orders", "deliveries", "day", "options", "switch"):
        assert command in output


def test_cli_switch_help_mentions_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.run_cli(["switch", "--help"])

    assert exit_code == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_cli_missing_session_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(session_path=tmp_path / "state" / "session.json")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    exit_code = cli.run_cli(["orders"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "dietly-mcp login" in captured.err
    assert "Traceback" not in captured.err


def test_cli_missing_credentials_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(credentials_path=tmp_path / "cfg" / "credentials.env")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    exit_code = cli.run_cli(["login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No credentials file" in captured.err
    assert "chmod 600" in captured.err
    assert "Traceback" not in captured.err


def test_cli_bad_date_is_a_clean_error(
    cli_client: RecordingClient, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.run_cli(["day", "--date", "15-06-2026"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Invalid --date" in captured.err


# --------------------------------------------------------------------------- #
# End-to-end: real CLI + real client + real session file, mocked HTTP layer
# --------------------------------------------------------------------------- #
@pytest.fixture
def e2e(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, transport: httpx.MockTransport
) -> None:
    """Route every CLI-created session through the mock transport."""
    original = Session._new_client
    mock_transport = transport

    def patched(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        return original(settings, transport=mock_transport)

    monkeypatch.setattr(Session, "_new_client", staticmethod(patched))
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    settings.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.credentials_path.write_text(
        "DIETLY_EMAIL=user@example.com\nDIETLY_PASSWORD=secret\n", encoding="utf-8"
    )
    settings.credentials_path.chmod(0o600)


@pytest.fixture
def e2e_routes(responses: dict[str, httpx.Response]) -> dict[str, httpx.Response]:
    """Register every route the end-to-end flow needs."""
    switch_path = (
        f"/api/company/customer/order/{ORDER_ID}/deliveries/{DELIVERY_ID}"
        f"/delivery-meals/{SLOT_ID}/switch"
    )
    responses["POST /api/auth/login"] = httpx.Response(
        200, headers={"set-cookie": "SESSION=abc123; Domain=dietly.pl; Path=/; HttpOnly"}
    )
    responses["GET /api/profile"] = httpx.Response(200, json={"email": "user@example.com"})
    responses["GET /api/profile/profile-order/all"] = httpx.Response(
        200, json={**ORDERS_PAGE_0, "totalPages": 1}
    )
    responses[f"GET /api/company/customer/order/{ORDER_ID}"] = httpx.Response(
        200, json=ORDER_DETAIL
    )
    responses[f"GET /api/company/general/menus/delivery/{DELIVERY_ID}/new"] = httpx.Response(
        200, json=DAY_MENU
    )
    responses[f"GET {switch_path}"] = httpx.Response(200, json=SWITCH_OPTIONS)
    responses[f"PUT {switch_path}"] = httpx.Response(200, json=SWITCH_RESPONSE)
    return responses


def test_end_to_end_login_status_orders_day_options_switch(
    e2e: None,
    e2e_routes: dict[str, httpx.Response],
    settings: Settings,
    seen: list[httpx.Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. login — writes the cookie jar
    assert cli.run_cli(["login"]) == 0
    assert "OK, logged in as user@example.com" in capsys.readouterr().out
    assert settings.session_path.exists()
    assert stat.S_IMODE(settings.session_path.stat().st_mode) == 0o600

    # 2. status — session is valid and the active order is listed
    assert cli.run_cli(["status"]) == 0
    status_out = capsys.readouterr().out
    assert "Session: valid (user@example.com)" in status_out
    assert str(ORDER_ID) in status_out

    # 3. orders --json
    assert cli.run_cli(["orders", "--json"]) == 0
    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload[0]["orderId"] == ORDER_ID
    assert payload[0]["dietCalories"] == 3000

    # 4. deliveries
    assert cli.run_cli(["deliveries"]) == 0
    deliveries_out = capsys.readouterr().out
    assert "2026-06-15" in deliveries_out
    assert "2026-06-16" in deliveries_out

    # 5. day (single active order → auto-selected, no --order needed)
    assert cli.run_cli(["day", "--date", "2026-06-15"]) == 0
    day_out = capsys.readouterr().out
    assert "Śniadanie" in day_out
    assert "Obiad" in day_out
    assert "Pasta jajeczna z pieczywem" in day_out

    # 6. options
    assert cli.run_cli(["options", "--date", "2026-06-15", "--slot", "Śniadanie"]) == 0
    options_out = capsys.readouterr().out
    assert "SPORT" in options_out
    assert "OPTIMAL" in options_out

    # 7. switch without --apply sends no PUT
    assert (
        cli.run_cli(["switch", "--date", "2026-06-15", "--slot", "Śniadanie", "--to", "2354"]) == 0
    )
    dry_run_out = capsys.readouterr().out
    assert "DRY RUN" in dry_run_out
    assert [r.method for r in seen].count("PUT") == 0

    # 8. switch --apply performs exactly one PUT with the right query
    assert (
        cli.run_cli(
            [
                "switch",
                "--date",
                "2026-06-15",
                "--slot",
                "Śniadanie",
                "--to",
                "2354",
                "--apply",
            ]
        )
        == 0
    )
    applied_out = capsys.readouterr().out
    assert "Switched Śniadanie on 2026-06-15" in applied_out
    puts = [request for request in seen if request.method == "PUT"]
    assert len(puts) == 1
    assert puts[0].url.params["dietCaloriesMealId"] == "2354"
    assert puts[0].url.params["amount"] == "1"
    assert puts[0].headers["company-id"] == "wybormenu"
    assert "SESSION=abc123" in puts[0].headers.get("cookie", "")


def test_end_to_end_expired_session_says_to_log_in(
    e2e: None,
    e2e_routes: dict[str, httpx.Response],
    seen: list[httpx.Request],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even the stored session is rejected — the message must tell the user what to do."""
    e2e_routes["GET /api/profile/profile-order/all"] = httpx.Response(401, json={})
    e2e_routes["GET /api/profile"] = httpx.Response(401, json={})

    exit_code = cli.run_cli(["orders"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "dietly-mcp login" in captured.err
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# Model serialisation
# --------------------------------------------------------------------------- #
def test_order_model_accepts_status_objects() -> None:
    order = Order.model_validate({"orderId": 5, "status": {"name": "ACTIVE"}})
    assert order.is_active is True


def test_order_to_dict_uses_api_field_names_and_drops_unknowns() -> None:
    order = Order.model_validate({**ORDERS_PAGE_0["content"][0], "someNewField": 1})
    dumped = order.to_dict()

    assert dumped["orderId"] == ORDER_ID
    assert dumped["companyName"] == "wybormenu"
    assert dumped["dateFrom"] == "2026-06-03"
    assert "someNewField" not in dumped
    assert "order_id" not in dumped


def test_meal_option_to_dict_keeps_target_id_at_top_level() -> None:
    option = MealOption.model_validate(SWITCH_OPTIONS["mealChangeOptions"][1])
    dumped = option.to_dict()

    assert dumped["dietCaloriesMealId"] == 2354
    assert dumped["dietOptionName"] == "OPTIMAL"
    assert dumped["menuMealName"] == "Jajecznica z pomidorami"


def test_switch_result_round_trips_api_response() -> None:
    result = SwitchResult.model_validate(SWITCH_RESPONSE)

    assert result.delivery_meal_id == SLOT_ID
    assert result.diet_calories_meal_id == 2354
    assert result.seen == "SWITCHED"
    assert result.to_dict()["deliveryMealId"] == SLOT_ID
