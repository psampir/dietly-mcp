# Dietly private API — notes used by this project

> **Attribution:** this is our own summary of the API contract, based on the MIT-licensed
> reverse-engineering notes in
> [jtomaszewski/dietly-autopilot — `docs/dietly-api.md`](https://github.com/jtomaszewski/dietly-autopilot/blob/main/docs/dietly-api.md)
> (see [THIRD_PARTY.md](../THIRD_PARTY.md)). No code was copied. Dietly has no public API and this
> contract can change at any time.

## Transport and headers

After login the app talks to `https://dietly.pl/api/...`. Every call carries:

| Header | Value |
|---|---|
| `accept` | `application/json` |
| `accept-language` | `pl-PL,pl;q=0.9,en;q=0.8` |
| `x-launcher-type` | `BROWSER_DIETLY` |
| `user-agent` | a believable recent desktop Firefox |
| `company-id` | catering slug (e.g. `wybormenu`) |

The `user-agent` matters because the site is behind Cloudflare: a Python-default UA tends to get
challenged (403/503 or a "Just a moment…" page). `dietly-mcp` sends a Firefox-on-Linux UA.

`company-id` is **per catering**, not per account:
account-wide endpoints (`/api/profile`, `/api/profile/profile-order/all`) work under any slug and
return orders from every catering, while order-scoped endpoints reject an order that belongs to a
different catering with **HTTP 490**. `dietly-mcp` therefore reads each order's catering from the
orders list (`companyName`) and falls back to `DIETLY_COMPANY_ID` (default `wybormenu`) when the
field is missing.

## Auth

- `POST https://panel.dietly.pl/api/auth/login`, body `application/x-www-form-urlencoded`:
  `username=<email>&password=<password>`.
- `200` with an empty body; the response sets an httpOnly session cookie. No MFA.
- The cookie jar is reused for all later calls and is persisted by us as JSON.
- Sanity check: `GET https://dietly.pl/api/profile` → `200` when the session is alive.
  (Noted in the original notes as also exposing `GET /api/profile/dietary-exclusions`, which we do
  not use yet.)

## Read endpoints

| Call | Returns |
|---|---|
| `GET /api/profile/profile-order/all?page=0` | all orders, paginated (`totalPages`). Active entries carry `orderId`, `status: "ACTIVE"`, `dateFrom`, `dateTo`, `companyName`, `dietCalories`, `dietName` |
| `GET /api/profile/profile-order/active-ids` | ids of active orders |
| `GET /api/company/customer/order/{orderId}` | order detail, incl. `deliveries[]` (`deliveryId`, `date` `YYYY-MM-DD`, `deliveryMeals[]` with `deliveryMealId` + `dietCaloriesMealId`) and `nearestDelivery.deliveryDates[]` |
| `GET /api/company/general/menus/delivery/{deliveryId}/new` | the day's chosen meals: `deliveryMenuMeal[]`, one per slot, with `mealName`, `menuMealName`, `deliveryMealId`, `dietCaloriesMealId`, `switchable`, `allergens[]`, `allergensWithExcluded[]`, `ingredients[]` (each ingredient has `name` and `exclusion[]` of `{dietaryExclusionId, name, chosen}`) |

Slot names (`mealName`) are Polish and used verbatim: `Śniadanie`, `II Śniadanie`, `Obiad`,
`Podwieczorek`, `Kolacja`.

## Switching a meal (the one write we support)

1. List the alternatives for one slot:
   `GET /api/company/customer/order/{orderId}/deliveries/{deliveryId}/delivery-meals/{deliveryMealId}/switch`
   → `mealChangeOptions[]`, each with:
   - `dietOptionName` — the variant line: `SPORT` / `OPTIMAL` / `SLIM` / `NISKI IG` / `WEGE`;
   - `menuMealDetails` — `menuMealName`, `dietCaloriesMealId` (**this is the id you send**),
     `allergens[]`, `allergensWithExcluded[]`, `ingredients[]`, `nutrition`, `mealName`;
   - `canBeChanged`, `mealRecommended`, `reviewSummary`.

2. Commit:
   `PUT …/deliveries/{deliveryId}/delivery-meals/{deliveryMealId}/switch?amount=1&dietCaloriesMealId={targetId}`
   → `200` with the updated meal (`deliveryMealId`, `amount`, `dietCaloriesMealId`,
   `seen: "SWITCHED"`).

Key invariants:

- `deliveryMealId` identifies the **slot** and never changes; only `dietCaloriesMealId` changes.
- `amount` is always `1` in the web app.
- Switches are reversible by sending the previous `dietCaloriesMealId` back, and the original
  notes report no side effects.

## Editing window

The calendar marks each day as `Edytuj` (editable), `Zobacz` (read-only, past cutoff) or
`Oceń` (already delivered). The cutoff is roughly **1.5 days before delivery**, so only days about
two days out or later can be edited; writes beyond the cutoff are rejected by the API.
F1 only reports what the API exposes (the flag is often absent, which we print as `-`) — deriving
or enforcing the cutoff belongs to a later phase.

## Failure modes we map to friendly errors

| Symptom | Meaning | Raised as |
|---|---|---|
| `401` | session cookie expired/invalid | `SessionExpiredError` → "run `dietly-mcp login`" |
| `403` (plain) | session rejected | `SessionExpiredError` |
| `403` / `503` with CF markers (`server: cloudflare`, `cf-ray`, "Just a moment…") | Cloudflare challenge | `CloudflareBlockedError` (mentions a possible `curl_cffi` fallback; not implemented in F1) |
| `490` | order belongs to another catering | `CateringMismatchError` |
| connection/TLS/timeout errors | never reached Dietly | `NetworkError` |
| other `4xx`/`5xx` | unexpected | `ApiError` with status, URL and parsed body |

## Phase 2 hook

The MCP server will not need any new HTTP code: FastMCP tools should wrap
`DietlyClient` (`src/dietly_mcp/client.py`) — `get_orders`, `get_order`, `get_day_menu`,
`get_switch_options`, `switch_meal(dry_run=False)` — and reuse the `DietlyError` hierarchy for
tool-level error messages. Model → JSON conversion is already available via
`ApiModel.to_dict()`.
