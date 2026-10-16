# dietly-mcp

Unofficial Python client for the **private JSON API of [Dietly](https://dietly.pl)** (panel.dietly.pl),
plus a debug CLI and a **read-only MCP server**. It lets you inspect active orders, look at the
meals chosen for a given delivery day, list the alternative meals offered for a slot, and switch a
single meal.

> **Phase 2 (F2)** = MCP server (read-only tools) + CLI polish. The server wraps
> `dietly_mcp.client.DietlyClient` and exposes **no write tool**; switching a meal stays on the CLI
> (`dietly-mcp switch`, dry-run unless `--apply`).

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
`DIETLY_EDIT_CUTOFF_HOURS` (editable heuristic, default `36`), `DIETLY_CREDENTIALS_FILE`,
`DIETLY_SESSION_FILE`.

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
.venv/bin/dietly-mcp switch  --date 2026-06-15 --slot Obiad --to 2354 --json    # DRY RUN, JSON plan
.venv/bin/dietly-mcp switch  --date 2026-06-15 --slot Obiad --to 2354 --apply   # real PUT
```

Notes:

- `--json` is available on `status`, `orders`, `deliveries`, `day`, `options` **and `switch`**
  (dry run and `--apply` alike) and emits API-shaped camelCase keys (unknown API fields are
  omitted).
- `orders` hides **historical** orders by default: Dietly keeps `status: "ACTIVE"` on finished
  orders, so a real activity check (`dateTo >= today`) is applied. Use `orders --all` to list
  everything.
- `deliveries` sorts days by date. When the API does not expose an `editable` flag, the column is
  marked as an **estimate** derived from `DIETLY_EDIT_CUTOFF_HOURS` (default `36`): a day is
  assumed editable when it starts at least that many hours from now. The same heuristic backs the
  MCP `dietly_deliveries` tool — it is only a heuristic, and Dietly can still reject a change.
- `--order <id>` disambiguates when several orders are active; with exactly one active order it
  is auto-selected. `orders` (and the MCP tools) hide the finished orders that Dietly still marks
  `ACTIVE`, so the active list is usually just the order you care about — pass `--order` only when
  you really have several running at once.
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

## MCP server

`dietly-mcp-server` speaks **MCP over stdio** and exposes five **read-only** tools. It wraps the
same `DietlyClient` as the CLI and never modifies the account (no write tools — meal switching is
CLI-only in this phase).

| Tool | Arguments | What it returns |
|------|-----------|-----------------|
| `dietly_status()` | — | session validity + email, and the genuinely-active order(s) |
| `dietly_orders()` | — | active orders (id, catering, diet, kcal, date range, deliveries count) |
| `dietly_deliveries(order_id?)` | optional order id | upcoming delivery days, sorted, with an **estimated** `editable` flag |
| `dietly_day(date, order_id?)` | `YYYY-MM-DD` / `today` / `tomorrow` | the meals chosen per slot (slot, dish, kcal, ids, switchable) |
| `dietly_options(date, slot, order_id?)` | date + slot name (e.g. `Obiad`) | alternatives for one slot (dish, variant, target id, kcal, allergens, `current`) |

Design notes:

- The session is reused from `~/.local/state/dietly-mcp/session.json`; when it is missing or
  expired the server **logs in automatically** from the credentials file. Failures come back as
  `{"ok": false, "error": ..., "hint": ...}` (never a traceback) — e.g. an expired session tells
  the caller to run `dietly-mcp login`.
- `editable` on `dietly_deliveries` is a **heuristic** (`DIETLY_EDIT_CUTOFF_HOURS`, default `36`),
  not an API guarantee; a write can still be rejected by Dietly.
- Polish data (`Obiad`, `Śniadanie`, dish names, allergens) is returned **verbatim**.

### OpenCode (V2)

Add the server to your OpenCode config (`~/.config/opencode/opencode.jsonc` for one project, or
`~/.config/opencode/opencode.json` globally):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "dietly": {
        "type": "local",
        "command": ["/home/pawel/Code/Projects/dietly-mcp/.venv/bin/dietly-mcp-server"],
        "codemode": false
      }
    }
  }
}
```

- OpenCode names each tool `<server>_<tool>`, so the tools appear as `dietly_status`,
  `dietly_orders`, `dietly_deliveries`, `dietly_day` and `dietly_options`.
- `codemode` defaults to `true`; with it, tools are reached through the Code Mode `tools` object
  (`tools.dietly.dietly_status(...)`) rather than as native tools. We set `codemode: false` so
  they stay **native** and permissions/allow-lists can target `dietly_*` directly.
- Optional `timeout` overrides exist (defaults: startup `30s`, catalog `30s`, execution `12h`).

You can add it from the CLI instead of editing the file:

```fish
# project config (add --global for every project)
opencode mcp add dietly -- /home/pawel/Code/Projects/dietly-mcp/.venv/bin/dietly-mcp-server

# verify the server starts and its tools are discovered
opencode mcp list
```

## Tests

```fish
.venv/bin/pytest          # or: uv run pytest
```

All tests use `httpx.MockTransport`; the suite never touches the network and needs no credentials.
`tests/test_client.py` also runs a full end-to-end flow (login → status → orders → day → options →
switch dry-run → switch apply) against the mocked HTTP layer, plus CSRF-rotation handling.
`tests/test_mcp_server.py` drives the registered MCP tools over the SDK's in-memory transport with
a fake client; `tests/test_logic.py` and `tests/test_cli_polish.py` cover the shared date/editable
policy and the CLI polish.

## Layout

```
src/dietly_mcp/
├── config.py      # paths, credentials file parsing, warnings, settings
├── http.py        # IPv4-pinned transport, explicit timeouts, bounded retries
├── session.py     # CSRF handshake, login, cookie-jar persistence (0600), validation
├── client.py      # DietlyClient — read calls + the single guarded switch
├── logic.py       # shared pure policy: date parsing, order activity, editable heuristic
├── models.py      # pydantic models mirroring the API
├── errors.py      # DietlyError hierarchy
├── mcp_server.py  # read-only MCP server (stdio) exposing five tools
└── cli.py         # Typer app (`dietly-mcp`)
```

## Status of this phase (F2)

Implemented and covered by mocked tests: everything from F1 (credentials loading, the CSRF login
handshake, cookie persistence, orders, deliveries, day menus, switch options, and the guarded
single-meal switch) **plus** the read-only MCP server (`dietly_status`, `dietly_orders`,
`dietly_deliveries`, `dietly_day`, `dietly_options`) and the shared date/editable policy used by
both the CLI and the MCP tools. The live flow (status → orders → deliveries → day → options →
switch dry run) and the MCP server over stdio have been verified against the real API on
2026-10-16; real writes are still intentionally left to the user via `--apply`.

Phase 3 (F3) will add **write** MCP tools (switch a meal, and `--json` parity for them) on top of
the same guarded `DietlyClient.switch_meal` path.

