# dietly-mcp

Unofficial Python client for the **private JSON API of [Dietly](https://dietly.pl)** (panel.dietly.pl),
plus a debug CLI. It lets you inspect active orders, look at the meals chosen for a given
delivery day, list the alternative meals offered for a slot, and switch a single meal.

> **Phase 1 (F1)** = API client + debug CLI. There is **no MCP server yet** — FastMCP tools
> arrive in phase 2 and will wrap `dietly_mcp.client.DietlyClient` unchanged.

This project is not affiliated with Dietly. It talks to an undocumented private API that may
change without notice. Use it on your own account only.

- API contract knowledge: [THIRD_PARTY.md](THIRD_PARTY.md)
- Reverse-engineered endpoint notes: [docs/api-notes.md](docs/api-notes.md)

---

## Install

Requires Python 3.12+ and (optionally) [uv](https://docs.astral.sh/uv/).

```fish
cd ~/Code/Projects/dietly-mcp

# with uv (recommended — uses the committed uv.lock, installs project + pytest)
uv sync --extra dev

# without uv
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Run it as `.venv/bin/dietly-mcp …` or `uv run dietly-mcp …` (fish-friendly: no `source activate` needed).

## Credentials

Credentials are read from a file **outside the repo** — never commit secrets:

```fish
mkdir -p ~/.config/dietly-mcp
printf '%s\n' 'DIETLY_EMAIL=you@example.com' 'DIETLY_PASSWORD=your-password' > ~/.config/dietly-mcp/credentials.env
chmod 600 ~/.config/dietly-mcp/credentials.env
```

The file is parsed as plain `KEY=VALUE` lines (it is **not** shell-sourced). If the permissions are
looser than `600`, every command prints a large warning on stderr. `DIETLY_EMAIL` /
`DIETLY_PASSWORD` environment variables are used as a fallback.

The session cookie is stored in `~/.local/state/dietly-mcp/session.json` (directory `700`, file `600`)
and never contains your password.

Optional overrides: `DIETLY_COMPANY_ID` (default `wybormenu`), `DIETLY_BASE_URL`,
`DIETLY_LOGIN_URL`, `DIETLY_USER_AGENT`, `DIETLY_ACCEPT_LANGUAGE`, `DIETLY_TIMEOUT`
(body/read timeout), `DIETLY_CONNECT_TIMEOUT`, `DIETLY_RETRIES`, `DIETLY_IPV4_ONLY`,
`DIETLY_CREDENTIALS_FILE`, `DIETLY_SESSION_FILE`.

### Network behaviour

- **IPv4 only by default** (`DIETLY_IPV4_ONLY=0` to opt out). Broken IPv6 routes are a common
  cause of instant "could not connect" errors or read timeouts; the client binds its sockets to
  `0.0.0.0` so every connection is `AF_INET`.
- **Explicit timeouts**: `DIETLY_CONNECT_TIMEOUT` (default `10`s) for dialing, `DIETLY_TIMEOUT`
  (default `30`s) for reading the response.
- **Small bounded retries** (`DIETLY_RETRIES`, default `2`) for transient connect/read errors only.

### Login (why two requests?)

Dietly's panel is a Spring app with a **cookie-based CSRF token**, so `dietly-mcp login` performs
the same handshake a browser does:

1. `GET /api/auth/login` → `401` **and** a `XSRF-TOKEN` cookie;
2. `POST /api/auth/login` (form body `username`/`password`) with `X-XSRF-TOKEN: <cookie value>`.

Posting without step 2's header returns **HTTP 403** with a Spring JSON body even when the password
is correct — that is a CSRF rejection, not bad credentials. `dietly-mcp` says so explicitly, and
re-primes/retries once if the server rotates the token.


## CLI usage

```fish
.venv/bin/dietly-mcp login                       # log in, persist the session cookie
.venv/bin/dietly-mcp status                      # session validity + active orders
.venv/bin/dietly-mcp orders --json               # machine-readable order list
.venv/bin/dietly-mcp deliveries                  # upcoming delivery days (all active orders)
.venv/bin/dietly-mcp day --date 2026-06-15       # chosen meals per slot (today/tomorrow also work)
.venv/bin/dietly-mcp options --date 2026-06-15 --slot Obiad   # alternatives for one slot
.venv/bin/dietly-mcp switch  --date 2026-06-15 --slot Obiad --to 2354           # DRY RUN
.venv/bin/dietly-mcp switch  --date 2026-06-15 --slot Obiad --to 2354 --apply   # real PUT
```

Notes:

- `--json` is available on `status`, `orders`, `deliveries`, `day` and `options` and emits
  API-shaped camelCase keys (unknown API fields are omitted).
- `--order <id>` disambiguates when several orders are active; with exactly one active order it
  is auto-selected. Note that Dietly keeps `status: "ACTIVE"` on finished orders too, so on most
  accounts you will need `--order` (check `dietly-mcp orders`).
- `--slot` takes the slot name **exactly as Dietly spells it**: `Śniadanie`, `II Śniadanie`,
  `Obiad`, `Podwieczorek`, `Kolacja` (matching is case-insensitive, values are never translated).
- **`switch` is a dry run by default.** It prints the current → target meal, both ids and the
  exact `PUT` URL it would send, and validates that the target id is among the options the API
  offers. Only `--apply` performs the switch.
- Expected errors print one clean message instead of a traceback, and are classified precisely:
  **401** → bad email/password; **403 + Spring JSON** → missing/rotated CSRF token; **403 with
  `cf-mitigated` or a "Just a moment…" page** → Cloudflare challenge; **403** otherwise → session
  problem; **490** → wrong `company-id`; connect/read failures → network error. Backend JSON bodies
  are quoted in the message so a failure is self-explanatory.

## Tests

```fish
.venv/bin/pytest          # or: uv run pytest
```

All tests use `httpx.MockTransport`; the suite never touches the network and needs no credentials.
`tests/test_client.py` also runs a full end-to-end flow (login → status → orders → day → options →
switch dry-run → switch apply) against the mocked HTTP layer, plus CSRF-rotation handling.

## Layout

```
src/dietly_mcp/
├── config.py    # paths, credentials file parsing, warnings, settings
├── http.py      # IPv4-pinned transport, explicit timeouts, bounded retries
├── session.py   # CSRF handshake, login, cookie-jar persistence (0600), validation
├── client.py    # DietlyClient — read calls + the single guarded switch
├── models.py    # pydantic models mirroring the API
├── errors.py    # DietlyError hierarchy
└── cli.py       # Typer app (`dietly-mcp`)
```

## Status of this phase (F1)

Implemented and covered by `MockTransport` tests: credentials loading, the CSRF login handshake,
cookie persistence, orders, deliveries, day menus, switch options, and the guarded single-meal
switch (dry run by default). The live login flow (login → status → orders → deliveries → day →
options → switch dry run) has been verified against the real API on 2026-10-16; real writes are
still intentionally left to the user via `--apply`.

The MCP server is phase 2. It hooks into `DietlyClient` from `src/dietly_mcp/client.py`; see the
note at the end of `docs/api-notes.md`.

