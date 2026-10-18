# dietly-mcp

An unofficial [MCP](https://modelcontextprotocol.io) server — plus a CLI — that lets an AI agent
read your **Dietly** delivery days and pick or switch meals. Writes are *safe by default*: a dry run
sends nothing, every attempt is audited, and any switch can be reverted.

[![CI](https://github.com/psampir/dietly-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/psampir/dietly-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **Unofficial.** This project talks to an *undocumented private API* that may change at any time.
> It is not affiliated with or endorsed by Dietly. Use it on your own account only.
>
> Deeper references: the reverse-engineered endpoint notes are in
> [docs/api-notes.md](docs/api-notes.md); the API-contract attribution is in
> [THIRD_PARTY.md](THIRD_PARTY.md).

## What you can do

- **See your orders and upcoming days** — which delivery days are ahead for your active order(s),
  with a best-effort *editable* estimate per day.
- **See the meals chosen for a day** — the dish picked for every slot (`Śniadanie`, `Obiad`, …),
  with calories and full macros.
- **List the alternatives for a slot** — every meal Dietly offers for that day/slot, with kcal,
  macros and allergens, and the id needed to switch to it.
- **Ask an agent to switch a meal** — with your approval, and only after it shows you a preview.
- **Revert** a switch from the audit log if you change your mind.
- **Everything is audited** — dry runs, applies and reverts, success or failure, in one append-only log.

## Quick start (Docker/Podman)

The canonical way to run it: a long-lived container serving MCP over Streamable HTTP.

```sh
podman pull ghcr.io/psampir/dietly-mcp:0.2.0      # or :latest

podman run -d --name dietly-mcp --restart=unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -e MCP_AUTH_TOKEN=<a-long-random-token> \
  -e DIETLY_EMAIL=you@example.com -e DIETLY_PASSWORD=<your-password> \
  -v dietly-data:/data \
  ghcr.io/psampir/dietly-mcp:0.2.0

curl -s http://127.0.0.1:8787/healthz             # 200 JSON, no auth, no account data
```

Generate a token with `openssl rand -hex 32`. A token is **mandatory** if you ever bind to anything
other than loopback.

Instead of `-e` variables you can mount a credentials file (the image already looks in `/config`):

```sh
mkdir -p ~/.config/dietly-mcp
printf '%s\n' 'DIETLY_EMAIL=you@example.com' 'DIETLY_PASSWORD=<your-password>' \
  > ~/.config/dietly-mcp/credentials.env
chmod 600 ~/.config/dietly-mcp/credentials.env
# add to the run command:  -v ~/.config/dietly-mcp:/config:ro
```

The `dietly-data` volume keeps the login session and the audit log across restarts and updates.

Ready-made recipes — docker-compose, a **Podman quadlet** (systemd) unit, reverse-proxy/TLS notes,
token rotation and a troubleshooting table — are in [docs/deploy.md](docs/deploy.md).

## Connect a client

### Open WebUI

Open WebUI (v0.6.31+) speaks native MCP over Streamable HTTP:

1. **Admin → Integrations → External Tool Servers → Add Connection.**
2. **Type:** *MCP (Streamable HTTP)*.
3. **URL:** `http://<host>:8787/mcp` (or `https://dietly.example.com/mcp` behind a proxy).
4. **Auth:** *Bearer* with the `MCP_AUTH_TOKEN` you set.

If Open WebUI runs in a container and the MCP server is on the host, use
`http://host.docker.internal:8787/mcp` instead of `localhost`.

To start conservatively, put only the **read** tools in the connection's *Function Name Filter
List* — `dietly_status`, `dietly_orders`, `dietly_deliveries`, `dietly_day`, `dietly_options` — and
add the write tools (`dietly_switch`, `dietly_apply`, `dietly_revert`) once you trust the setup.

### Other MCP clients (stdio)

Any client that spawns MCP servers over stdio works with the `dietly-mcp-server` console script:

```json
{
  "mcpServers": {
    "dietly": { "command": "dietly-mcp-server" }
  }
}
```

If the project is not installed globally, point at it with `uv` or an absolute path:

```json
{
  "mcpServers": {
    "dietly": { "command": "uv", "args": ["run", "--directory", "/path/to/dietly-mcp", "dietly-mcp-server"] }
  }
}
```

Most clients expose each tool as `<server>_<tool>` (so with a server named `dietly` they appear as
`dietly_status` … `dietly_revert`). stdio needs **no token** — there is no network surface. Client-
specific setup (including OpenCode's `codemode` flag) is in [docs/deploy.md](docs/deploy.md).

## Tools reference

`dietly-mcp-server` exposes eight tools: five read-only and three write. The write tools share the
CLI's write path, so the guardrails, dry-run semantics and audit records cannot drift between them.

**Read tools** (never modify anything):

| Tool | Arguments | What it returns |
|------|-----------|-----------------|
| `dietly_status()` | — | session validity + email, and the genuinely-active order(s) |
| `dietly_orders()` | — | active orders (id, catering, diet, kcal, date range, deliveries count) |
| `dietly_deliveries(order_id?)` | optional order id | upcoming delivery days, sorted, with an **estimated** `editable` flag |
| `dietly_day(date, order_id?)` | `YYYY-MM-DD` / `today` / `tomorrow` | the meals chosen per slot (slot, dish, kcal, `macros`, ids, switchable, allergens) |
| `dietly_options(date, slot, order_id?)` | date + slot name (e.g. `Obiad`) | alternatives for one slot (dish, variant, target id, kcal, `macros`, allergens, `current`); `current` is a summary of the selected dish (`dish`, `targetId`, `macros`) |

**Write tools** — these **modify your real Dietly order**. All three are dry-run by default
(`dry_run=True`) and only write when called with `dry_run=False`; every attempt is audited.

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `dietly_switch(date, slot, to_id, order_id?, dry_run=True)` | date, slot, target meal id (int or digit string) | previews (dry run) or performs **one** switch; refuses a target that was not offered, listing the valid ids |
| `dietly_apply(entries, order_id?, dry_run=True)` | `[{"date": "2026-11-02", "slot": "Kolacja", "to_id": 3648}, …]` | validates **all** entries first (fail fast with a list of problems, nothing sent), then previews or applies them sequentially. A real run never aborts silently: each result has `ok`/`error` + its `auditId`, and the payload summarises `applied` vs `failed` |
| `dietly_revert(audit_id?, date?, slot?)` | audit entry id, or date/slot filter | undoes a recorded switch (**a real write**). Refuses when the recorded dish is gone or the day looks locked; the revert is itself audited |

The write payloads carry the order/date/slot context, the `from`/`to` selections (id, name, kcal,
`macros`), the exact `PUT` request and the `auditId` of the record. Dry runs add `"dryRun": true` and
never send a request. Failures come back as `{"ok": false, "error": ..., "hint": ...}` (never a
traceback) — e.g. an expired session tells the caller to run `dietly-mcp login`. The session is
reused from the cookie jar, and the server **logs in automatically** when it is missing or expired.
Write calls are **serialised** (a process-wide lock), so two switches can never race. Polish data
(`Obiad`, `Śniadanie`, dish names, allergens) is returned **verbatim**.

### Nutrition / macros

Every meal in a read or write payload carries the **full nutrition** Dietly returns for a portion,
not just the calorie figure. Beside the top-level `kcal` (kept for compatibility) there is a compact
`macros` object:

```json
"macros": {
  "kcal": 427, "protein": 38.1, "carbs": 33.4, "fat": 15.5,
  "fiber": 6.3, "sugar": 3.1, "salt": 1.2, "saturates": 2.2,
  "weight": 535, "kcalText": "427 kcal / 1788 kJ"
}
```

`protein`/`carbs`/`fat`/`fiber`/`sugar`/`salt`/`saturates` are **grams** (rounded to one decimal),
`weight` is grams, and `kcalText` is the API's own label. `carbs` comes from the API's
`carbohydrate`, `fiber` from `dietaryFiber`, and `saturates` from `saturatedFattyAcids`; keys the API
omits are omitted here too. `macros` is present in `dietly_day` (per slot), `dietly_options` (per
alternative **and** on the top-level `current` summary), and on both the `from` and `to` sides of
`dietly_switch`/`dietly_apply` — preview **and** result — so a switch is approved on full nutrition.
The CLI shows the same figures compactly (`544 kcal | B 38g / W 33g / T 15g`) in `day`, `options`
**and the `switch` plan**; `--json` there keeps the raw `nutrition` dict as well. Blank allergen
entries (the API's `[""]`) are stripped from every payload.

## Safety: how writes are guarded

The write tools **modify your real Dietly order**. Four rules keep that safe:

1. **Dry-run by default.** `dietly_switch`/`dietly_apply` and `dietly-mcp switch` send nothing unless
   you explicitly pass `dry_run=False` / `--apply`. A dry run only performs GETs and returns a preview
   (current dish → target dish, both ids, kcal, macros, and the order/date/slot context).
2. **Show the preview, then approve.** An agent driving these tools must call the write tool with the
   default dry run first, show that preview to the user, and get explicit approval before repeating
   the call with `dry_run=False`.
3. **Audit + revert.** Every attempt — dry run, apply, revert, success or failure — is appended to
   the audit log (`0600`, append-only, `DIETLY_AUDIT_FILE` to relocate). Each record keeps the
   previous and the new selection, and `dietly_revert` (or `dietly-mcp revert`) restores the previous
   dish from that record. A revert is only issued when the recorded dish is still offered and the
   day still looks open; it is itself audited.
4. **The cutoff is a heuristic.** The `editable` flag is estimated from `DIETLY_EDIT_CUTOFF_HOURS`
   (default `36`) and the delivery date — it is *not* an API guarantee, and the server may still
   reject a write (or accept one the estimate called locked). Writes are never retried blindly.

Targets are validated before anything is sent: if the requested meal id is not among the options
Dietly offers for that slot, the call fails and the error lists the valid ids.

## Configuration

Credentials are read from a file that lives **outside the repo** — never commit secrets. The file is
parsed as plain `KEY=VALUE` lines (it is **not** shell-sourced); if its permissions are looser than
`600`, every command prints a large warning on stderr. `DIETLY_EMAIL` / `DIETLY_PASSWORD` environment
variables are used as a fallback.

| Variable | Default | Purpose |
|---|---|---|
| `DIETLY_EMAIL` / `DIETLY_PASSWORD` | — | credentials (env fallback; `DIETLY_USERNAME` also accepted) |
| `DIETLY_CREDENTIALS_FILE` | `~/.config/dietly-mcp/credentials.env` | credentials file (`KEY=VALUE`, not shell-sourced) |
| `DIETLY_SESSION_FILE` | `~/.local/state/dietly-mcp/session.json` | cookie jar (written `0600`; never contains the password) |
| `DIETLY_AUDIT_FILE` | `~/.local/state/dietly-mcp/audit.jsonl` | append-only write log (`0600`) |
| `DIETLY_CONFIG_DIR` | `$XDG_CONFIG_HOME/dietly-mcp` | replaces the whole config dir (container: `/config`) |
| `DIETLY_STATE_DIR` | `$XDG_STATE_HOME/dietly-mcp` | replaces the whole state dir (container: `/data`) |
| `DIETLY_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `DIETLY_HTTP_HOST` | `127.0.0.1` | HTTP bind address (a **non-loopback** bind requires `MCP_AUTH_TOKEN`) |
| `DIETLY_HTTP_PORT` | `8787` | HTTP port |
| `MCP_AUTH_TOKEN` | — | bearer token required on every MCP request; mandatory for non-loopback binds |
| `DIETLY_TIMEOUT` | `30` | response read timeout (seconds) |
| `DIETLY_CONNECT_TIMEOUT` | `10` | connection (dial) timeout (seconds) |
| `DIETLY_RETRIES` | `2` | bounded retries for transient connect/read errors only |
| `DIETLY_IPV4_ONLY` | `1` | bind sockets to IPv4 (`0` to opt out); broken IPv6 routes are a common cause of connect errors |
| `DIETLY_EDIT_CUTOFF_HOURS` | `36` | editable *heuristic*: days starting closer than this are assumed locked |
| `DIETLY_COMPANY_ID` | `wybormenu` | catering slug fallback for order-scoped calls |
| `DIETLY_BASE_URL`, `DIETLY_LOGIN_URL`, `DIETLY_USER_AGENT`, `DIETLY_ACCEPT_LANGUAGE` | panel.dietly.pl | advanced overrides (see [docs/api-notes.md](docs/api-notes.md)) |

The HTTP endpoint can modify your real order, so it is guarded: `MCP_AUTH_TOKEN` set means every
request except `GET /healthz` must send `Authorization: Bearer <token>` (anything else is answered
`401` before a tool runs); unset means only loopback binds are allowed and a wider bind is refused at
start-up. `GET /healthz` is public and reports booleans only — never an email, token, cookie or order
datum. The server boots without credentials and keeps serving `/healthz`.
[docs/deploy.md](docs/deploy.md) covers the bind/token matrix, endpoints and proxying in full.

## CLI

The same tools are available from the command line (a secondary surface for debugging). Run it as
`.venv/bin/dietly-mcp …` or `uv run dietly-mcp …`.

```sh
dietly-mcp login                                    # log in, persist the session cookie
dietly-mcp status                                   # session validity + active orders
dietly-mcp orders [--all]                           # running orders (--all includes history)
dietly-mcp deliveries [--order <id>]                # upcoming delivery days, with editable estimate
dietly-mcp day --date 2026-06-15                    # chosen meals per slot (today/tomorrow work too)
dietly-mcp options --date 2026-06-15 --slot Obiad   # alternatives for one slot
dietly-mcp switch --date 2026-06-15 --slot Obiad --to 2354          # DRY RUN
dietly-mcp switch --date 2026-06-15 --slot Obiad --to 2354 --apply  # real write
dietly-mcp revert [--audit-id <id>] [--dry-run]     # undo a recorded switch (real write)
dietly-mcp audit [--limit 10]                       # the write audit trail
dietly-mcp version                                  # print the version
```

- **`--json` is available everywhere** (`status`, `orders`, `deliveries`, `day`, `options`, `switch`,
  `revert`, `audit`) and emits API-shaped camelCase keys; unknown API fields are omitted.
- **`orders` hides historical orders by default** — Dietly keeps `status: "ACTIVE"` on finished
  orders, so a real activity check (`dateTo >= today`) is applied. Use `orders --all` for everything,
  and `--order <id>` to disambiguate when more than one order could be chosen.
- **`--slot` takes the slot name exactly as Dietly spells it** — `Śniadanie`, `II Śniadanie`,
  `Obiad`, `Podwieczorek`, `Kolacja` (matching is case-insensitive; values are never translated).
- **`switch` is a dry run** unless `--apply` is given; **`revert` is a real write** unless
  `--dry-run` is given. Expected errors print one clean message instead of a traceback.

## Development

Requires Python 3.12+ and (optionally) [uv](https://docs.astral.sh/uv/).

```sh
uv sync --extra dev              # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                 # or: uv run pytest
.venv/bin/ruff check .
```

All tests use `httpx.MockTransport`; the suite never touches the network and needs no credentials.
Layout:

```
src/dietly_mcp/
├── config.py      # paths (env-overridable, container-friendly), credentials, settings
├── http.py        # IPv4-pinned transport, explicit timeouts, bounded retries
├── session.py     # CSRF handshake, login, cookie-jar persistence (0600), validation
├── client.py      # DietlyClient — read calls + the guarded single switch
├── logic.py       # shared pure policy: date parsing, order activity, editable heuristic
├── models.py      # pydantic models mirroring the API
├── errors.py      # DietlyError hierarchy
├── audit.py       # append-only JSONL write log (0600) + lookups used by revert
├── operations.py  # shared write path: switch/apply/revert + payloads/error mapping
├── mcp_server.py  # MCP tools (5 read + 3 write), client cache/write locks, entry point
├── http_server.py # Streamable-HTTP transport: ASGI app, bearer gate, /healthz, bind policy
└── cli.py         # Typer app (`dietly-mcp`)
```

## License & credits

Released under the [MIT License](LICENSE) — `Copyright (c) 2026 Pawel Sampir`.

The reverse-engineered API knowledge used here is based on the MIT-licensed
[jtomaszewski/dietly-autopilot](https://github.com/jtomaszewski/dietly-autopilot) notes; no code was
copied. Full attribution and third-party notices are in [THIRD_PARTY.md](THIRD_PARTY.md).

This is an independent, unofficial client. "Dietly" and dietly.pl are the property of their
respective owners; this project is not affiliated with or endorsed by Dietly.
