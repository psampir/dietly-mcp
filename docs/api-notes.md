# Dietly private API — notes used by this project

> **Attribution:** this is our own summary of the API contract. It started from the MIT-licensed
> reverse-engineering notes in
> [jtomaszewski/dietly-autopilot — `docs/dietly-api.md`](https://github.com/jtomaszewski/dietly-autopilot/blob/main/docs/dietly-api.md)
> (see [THIRD_PARTY.md](../THIRD_PARTY.md)). No code was copied. Dietly has no public API and this
> contract can change at any time.
>
> **The auth section below is our own observation** (2026-10-16, verified against the live SPA): the
> upstream notes predate the CSRF requirement and document a one-request login that no longer works.

## Transport and headers

After login the app talks to `https://panel.dietly.pl/api/...`. Every call carries:

| Header | Value |
|---|---|
| `accept` | `application/json` |
| `accept-language` | `pl-PL,pl;q=0.9,en;q=0.8` |
| `x-launcher-type` | `BROWSER_DIETLY` |
| `user-agent` | a believable recent desktop Firefox |
| `company-id` | catering slug (e.g. `wybormenu`), only on order-scoped calls |
| `X-XSRF-TOKEN` | value of the `XSRF-TOKEN` cookie; required on state-changing calls |

> **Why `panel.dietly.pl` and not `dietly.pl`:** the login response sets a **host-only** `SESSION`
> cookie for `panel.dietly.pl`, so `https://dietly.pl/api/...` only works if the `remember-me`
> cookie (which *is* scoped to `.dietly.pl`) happens to be present. Verified live on 2026-10-16:
> with only `SESSION` in the jar, `dietly.pl/api/profile` → `401` while
> `panel.dietly.pl/api/profile` → `200`. The panel host is the canonical API host.

The `user-agent` matters because the site is behind Cloudflare: a Python-default UA tends to get
challenged (403/503 or a "Just a moment…" page). `dietly-mcp` sends a Firefox-on-Linux UA.

`company-id` is **per catering**, not per account. Account-wide endpoints (`/api/profile`,
`/api/profile/profile-order/all`) work under any slug and return orders from every catering, while
order-scoped endpoints reject an order that belongs to a different catering with **HTTP 490**.
`dietly-mcp` therefore reads each order's catering from the orders list (`companyName`) and falls
back to `DIETLY_COMPANY_ID` (default `wybormenu`) when the field is missing.

## Auth

Spring Security with a **cookie-based CSRF token**. The sequence the SPA performs — and that
`dietly-mcp login` reproduces — is:

1. **Prime the token.** `GET https://panel.dietly.pl/api/auth/login` (or any `/api/**` request)
   answers `401` **and** sets `XSRF-TOKEN=<36-char token>; Path=/` (not HttpOnly, so JS can read it).
2. **Log in.** `POST https://panel.dietly.pl/api/auth/login` with
   `application/x-www-form-urlencoded` body `username=<email>&password=<password>` **and**
   `X-XSRF-TOKEN: <value of the XSRF-TOKEN cookie>`.
   - `200` with `{"loyaltyActions":[]}`, and sets `SESSION` (host-only `panel.dietly.pl`),
     `remember-me` (`Domain=.dietly.pl`) and a **rotated** `XSRF-TOKEN`.
   - `401` with `text/plain` `Niepoprawne dane uwierzytelniające` → wrong email/password.
3. **Persist** the whole jar and reuse it for every later call, including the `X-XSRF-TOKEN`
   header on writes.

Skipping step 2's header yields **HTTP 403** with Spring's default error JSON
(`{"timestamp":…,"status":403,"error":"Forbidden","path":"/auth/login"}`) for *any* password,
including a correct one, and a fresh `XSRF-TOKEN` cookie. That is a CSRF rejection — it is **not**
bad credentials, and it is **not** Cloudflare (Cloudflare fronts every response, so
`server: cloudflare` alone means nothing).

Sanity check: `GET https://panel.dietly.pl/api/profile` → `200` when the session is alive.

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
| `401` (plain text / JSON) | wrong email or password (login), or session not authenticated (API) | `InvalidCredentialsError` / `SessionExpiredError` → "run `dietly-mcp login`" |
| `403` + Spring JSON (`status:403,error:Forbidden`) + a rotated `XSRF-TOKEN` cookie | Spring Security CSRF rejection (missing/rotated `X-XSRF-TOKEN`) | `CsrfTokenError` (client re-primes once, then reports) |
| `403` with `cf-mitigated` or an HTML "Just a moment…" page | Cloudflare challenge | `CloudflareBlockedError` (mentions a possible `curl_cffi` fallback; not implemented) |
| `403` (any other body) or `503` without Cloudflare markers | the backend refused the call | `SessionExpiredError` (quotes the backend JSON) |
| `490` | order belongs to another catering | `CateringMismatchError` |
| connection/TLS/timeout errors | never reached Dietly | `NetworkError` (after a couple of transient retries) |
| other `4xx`/`5xx` | unexpected | `ApiError` with status, URL and parsed body |

> `server: cloudflare` appears on **every** response, including plain `401`/`403` JSON errors, so it
> is deliberately *not* used as a challenge signal. Only `cf-mitigated` and HTML interstitials count.


## Phase 2 hook

The MCP server will not need any new HTTP code: FastMCP tools should wrap
`DietlyClient` (`src/dietly_mcp/client.py`) — `get_orders`, `get_order`, `get_day_menu`,
`get_switch_options`, `switch_meal(dry_run=False)` — and reuse the `DietlyError` hierarchy for
tool-level error messages. Model → JSON conversion is already available via
`ApiModel.to_dict()`.
