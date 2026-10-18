# dietly-mcp

Unofficial Python client for the **private JSON API of [Dietly](https://dietly.pl)** (panel.dietly.pl),
plus a debug CLI and an **MCP server**. It lets you inspect active orders, look at the meals chosen for
a given delivery day, list the alternative meals offered for a slot, and switch a single meal — safely:
**dry-run by default, every attempt audited, every switch revertible**.

> **Phase 3 (F3)** = MCP **write** tools (`dietly_switch`, `dietly_apply`, `dietly_revert`), an
> append-only audit log and CLI parity (`dietly-mcp revert`, `dietly-mcp audit`). The write path is
> shared with the CLI through `dietly_mcp.operations`, so both front ends refuse a target Dietly did
> not offer and never send anything on a dry run.

This project is not affiliated with Dietly. It talks to an undocumented private API that may
change without notice. Use it on your own account only.

- API contract knowledge: [THIRD_PARTY.md](THIRD_PARTY.md)
- Reverse-engineered endpoint notes: [docs/api-notes.md](docs/api-notes.md)

---

## Safety (read this before using a write tool)

The write tools **modify your real Dietly order**. Four rules keep that safe:

1. **Dry-run by default.** `dietly_switch`/`dietly_apply` and `dietly-mcp switch` send nothing unless
   you explicitly pass `dry_run=False` / `--apply`. A dry run only performs GETs and returns a preview
   (current dish → target dish, both ids, kcal, macros, and the order/date/slot context).
2. **Show the preview, then approve.** An agent driving these tools must call the write tool with the
   default dry run first, show that preview to the user, and get explicit approval before repeating
   the call with `dry_run=False`.
3. **Audit + revert.** Every attempt — dry run, apply, revert, success or failure — is appended to
   `~/.local/state/dietly-mcp/audit.jsonl` (`0600`, append-only, `DIETLY_AUDIT_FILE` to relocate).
   Each record keeps the previous and the new selection, and `dietly_revert` (or `dietly-mcp revert`)
   restores the previous dish from that record. A revert is only issued when the recorded dish is
   still offered and the day still looks open; it is itself audited.
4. **The cutoff is a heuristic.** The editable flag is estimated from `DIETLY_EDIT_CUTOFF_HOURS`
   (default `36`) and the delivery date — it is *not* an API guarantee, and the server may still
   reject a write (or accept one the estimate called locked). Writes are never retried blindly.

Targets are validated before anything is sent: if the requested meal id is not among the options
Dietly offers for that slot, the call fails and the error lists the valid ids.

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
and never contains your password. The write audit log lives next to it
(`~/.local/state/dietly-mcp/audit.jsonl`, mode `600`, append-only).

Optional overrides: `DIETLY_COMPANY_ID` (default `wybormenu`), `DIETLY_BASE_URL`,
`DIETLY_LOGIN_URL`, `DIETLY_USER_AGENT`, `DIETLY_ACCEPT_LANGUAGE`, `DIETLY_TIMEOUT`
(body/read timeout), `DIETLY_CONNECT_TIMEOUT`, `DIETLY_RETRIES`, `DIETLY_IPV4_ONLY`,
`DIETLY_EDIT_CUTOFF_HOURS` (editable heuristic, default `36`), `DIETLY_CREDENTIALS_FILE`,
`DIETLY_SESSION_FILE`, `DIETLY_AUDIT_FILE`.

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
.venv/bin/dietly-mcp revert                                                    # undo the last applied switch
.venv/bin/dietly-mcp revert --audit-id 3f2b… --dry-run                         # preview a specific undo
.venv/bin/dietly-mcp audit --limit 10                                          # last 10 audit entries
.venv/bin/dietly-mcp audit --json                                              # machine-readable audit trail
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
- `--order <id>` disambiguates when more than one order could be chosen. The read commands
  (`day`, `options`, `deliveries`) count every order Dietly reports as `ACTIVE` — and it keeps
  stamping finished orders that way, so on a long-lived account you usually need `--order` there.
  The write commands (`switch`, `revert`) and the MCP tools prefer the single **genuinely current**
  order (`status ACTIVE` *and* `dateTo >= today`) — the same list `orders` shows — and only fall back
  to the wider set when nothing is current.
- `--slot` takes the slot name **exactly as Dietly spells it**: `Śniadanie`, `II Śniadanie`,
  `Obiad`, `Podwieczorek`, `Kolacja` (matching is case-insensitive, values are never translated).
- **`switch` is a dry run by default.** It prints the current → target meal, both ids and the
  exact `PUT` URL it would send, and validates that the target id is among the options the API
  offers. Only `--apply` performs the switch. `--json` reports the audit entry id (`auditId`).
- **`revert` undoes a recorded switch** and is a **real write by default** (`--dry-run` to preview).
  With no arguments it reverts the most recent applied switch from the audit log; `--audit-id`
  picks a specific entry and `--date`/`--slot` narrow the search. It refuses — sending nothing — when
  the recorded dish is no longer offered or the day looks locked, and records the revert itself.
- **`audit` shows the write trail** (`--limit N`, `--json`): one line per attempt, including dry runs
  and failures, with the previous/new dish and the tool that made the call. The file is append-only
  (`~/.local/state/dietly-mcp/audit.jsonl`, mode `600`) and never contains credentials or tokens.
- Expected errors print one clean message instead of a traceback, and are classified precisely:
  **401** → bad email/password; **403 + Spring JSON** → missing/rotated CSRF token; **403 with
  `cf-mitigated` or a "Just a moment…" page** → Cloudflare challenge; **403** otherwise → session
  problem; **490** → wrong `company-id`; connect/read failures → network error. Backend JSON bodies
  are quoted in the message so a failure is self-explanatory.

## MCP server

`dietly-mcp-server` speaks **MCP over stdio** (default) or **Streamable HTTP** and exposes eight tools:
five **read-only** ones and three **write** ones. It wraps the same `DietlyClient` as the CLI, and the
write tools call the same `dietly_mcp.operations` functions as `dietly-mcp switch`/`revert` — the
guardrail, the dry-run semantics and the audit records cannot drift between the two front ends.

**Read tools** (never modify anything):

| Tool | Arguments | What it returns |
|------|-----------|-----------------|
| `dietly_status()` | — | session validity + email, and the genuinely-active order(s) |
| `dietly_orders()` | — | active orders (id, catering, diet, kcal, date range, deliveries count) |
| `dietly_deliveries(order_id?)` | optional order id | upcoming delivery days, sorted, with an **estimated** `editable` flag |
| `dietly_day(date, order_id?)` | `YYYY-MM-DD` / `today` / `tomorrow` | the meals chosen per slot (slot, dish, kcal, `macros`, ids, switchable, allergens) |
| `dietly_options(date, slot, order_id?)` | date + slot name (e.g. `Obiad`) | alternatives for one slot (dish, variant, target id, kcal, `macros`, allergens, `current`) |

**Write tools** — these **modify your real Dietly order**. All three are dry-run by default
(`dry_run=True`) and only write when called with `dry_run=False`; every attempt is audited.

| Tool | Arguments | What it does |
|------|-----------|--------------|
| `dietly_switch(date, slot, to_id, order_id?, dry_run=True)` | date, slot, target meal id (int or digit string) | previews (dry run) or performs **one** switch; refuses a target that was not offered, listing the valid ids |
| `dietly_apply(entries, order_id?, dry_run=True)` | `[{"date": "2026-10-29", "slot": "Kolacja", "to_id": 3648}, …]` | validates **all** entries first (fail fast with a list of problems, nothing sent), then previews or applies them sequentially. A real run never aborts silently: each result has `ok`/`error` + its `auditId`, and the payload summarises `applied` vs `failed` |
| `dietly_revert(audit_id?, date?, slot?)` | audit entry id, or date/slot filter | undoes a recorded switch (**a real write**). Refuses when the recorded dish is gone or the day looks locked; the revert is itself audited |

The write payloads carry the order/date/slot context, the `from`/`to` selections (id, name, kcal,
`macros`), the exact `PUT` request and the `auditId` of the record. Dry runs add `"dryRun": true` and
never send a request.

Design notes:

- The session is reused from `~/.local/state/dietly-mcp/session.json`; when it is missing or
  expired the server **logs in automatically** from the credentials file. Failures come back as
  `{"ok": false, "error": ..., "hint": ...}` (never a traceback) — e.g. an expired session tells
  the caller to run `dietly-mcp login`.
- `editable` on `dietly_deliveries` is a **heuristic** (`DIETLY_EDIT_CUTOFF_HOURS`, default `36`),
  not an API guarantee; a write can still be rejected by Dietly. `dietly_revert` uses the same
  heuristic to decide whether the day still looks open.
- **Agents: preview first.** Call a write tool with the default dry run, show the preview to the user,
  get explicit approval, then repeat with `dry_run=False`. Undo with `dietly_revert`.
- Polish data (`Obiad`, `Śniadanie`, dish names, allergens) is returned **verbatim**.
- Write tools are **serialised** (a process-wide lock): two switches can never race, and the
  client/session cache is guarded for concurrent requests.

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
alternative), and on both the `from` and `to` sides of `dietly_switch`/`dietly_apply` — preview
**and** result — so a switch is approved on full nutrition. The CLI shows the same figures compactly
(`544 kcal | B 38g / W 33g / T 15g`) in `day` and `options`; `--json` there keeps the raw `nutrition`
dict as well. Blank allergen entries (the live API's `[""]`) are stripped from every payload.

### Streamable HTTP transport

For a long-lived server (containers, remote clients such as Open WebUI) the same binary serves MCP
over **Streamable HTTP** at `/mcp`, plus a public `/healthz`:

```fish
dietly-mcp-server --transport streamable-http            # 127.0.0.1:8787 by default
dietly-mcp-server --transport streamable-http --host 0.0.0.0 --port 8787
```

| Option | Env | Default | Notes |
|---|---|---|---|
| `--transport` | `DIETLY_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `--host` | `DIETLY_HTTP_HOST` | `127.0.0.1` | a **non-loopback** bind requires `MCP_AUTH_TOKEN` |
| `--port` | `DIETLY_HTTP_PORT` | `8787` | |
| `--version` | — | | print the version and exit |

Safety (this endpoint can modify your real order):

- **`MCP_AUTH_TOKEN` set** → every request except `GET /healthz` must send
  `Authorization: Bearer <token>`; anything else is answered `401` before a tool runs.
- **unset** → only loopback binds are allowed; a wider bind is **refused at start-up** with a message
  that says exactly what to do.
- **No credentials needed to start**: the server boots and serves `/healthz` regardless, and tool
  calls return the usual actionable `{"ok": false, ...}` errors.
- `/healthz` (no auth) reports `service`, `version`, `transport`, `mcpPath`, `uptimeSeconds`,
  `authRequired` and the booleans `credentialsConfigured` / `sessionConfigured` — never an email,
  token, cookie or order datum.
- Logs go to **stdout** (containers, `podman logs`); stdio mode is untouched.

Config/state locations are container-friendly: `DIETLY_CONFIG_DIR` (e.g. `/config`) and
`DIETLY_STATE_DIR` (e.g. `/data`) replace the XDG roots; `DIETLY_CREDENTIALS_FILE`,
`DIETLY_SESSION_FILE` and `DIETLY_AUDIT_FILE` override single paths.

### Deployment (container)

A multi-stage image (non-root, `python:3.13-slim`, `uv sync --frozen`) is built and published to GHCR
by CI on every `v*` tag: `ghcr.io/psampir/dietly-mcp` (`X.Y.Z`, `X.Y`, `latest`; `linux/amd64` +
`linux/arm64`, with provenance and SBOM).

```fish
podman run -d --name dietly-mcp --restart=unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -e MCP_AUTH_TOKEN=(openssl rand -hex 32) \
  -e DIETLY_EMAIL=you@example.com -e DIETLY_PASSWORD=<your-password> \
  -v dietly-data:/data \
  ghcr.io/psampir/dietly-mcp:0.1.0

curl -s http://127.0.0.1:8787/healthz     # 200 JSON, no auth, no account data
```

Full recipes — credentials from env or a mounted `/config/credentials.env`, docker-compose, a
**Podman quadlet** unit, reverse-proxy/TLS notes (SSE needs `proxy_buffering off`), token rotation,
updating and a troubleshooting table — are in [docs/deploy.md](docs/deploy.md).

Publishing a release is just a tag (the workflow lints, tests, cross-builds and pushes, then creates
the GitHub Release):

```fish
git tag v0.1.0 && git push origin v0.1.0
```

After the **first** push, set the GHCR package visibility to **Public** once (GitHub → profile →
*Packages* → `dietly-mcp` → *Package settings*), or `podman login ghcr.io` before pulling.

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

- OpenCode names each tool `<server>_<tool>`, so the tools appear as `dietly_status`, `dietly_orders`,
  `dietly_deliveries`, `dietly_day`, `dietly_options`, `dietly_switch`, `dietly_apply` and
  `dietly_revert`.
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
`tests/test_mcp_server.py` drives the registered MCP tools — read *and* write — over the SDK's
in-memory transport with a fake client, and covers the shared write path: a dry run sends nothing
(zero applied writes), an unoffered target is refused with the valid ids, `apply` fails fast on shape
problems and reports per-entry `ok`/`error` on a partial failure, and revert refuses a gone dish or a
locked day. `tests/test_http_transport.py` drives the Streamable-HTTP app in-process (Starlette's
`TestClient`, no sockets): the bearer matrix (missing/wrong/valid), that a `401` never reaches the
account, "loopback without a token is fine but a wider bind is refused", the `/healthz` shape and that
it leaks nothing even with credentials and a session present, booting without credentials, flag/env
resolution, and that concurrent writes never overlap. `tests/test_audit.py` covers the append-only log
(mode `600`, junk lines, lookups) and `tests/test_cli_write.py` pins the CLI surface (`switch --json`,
`revert`, `audit`). `tests/test_logic.py` and `tests/test_cli_polish.py` cover the shared
date/editable policy and the CLI polish.

## Layout

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

Dockerfile .dockerignore
.github/workflows/{ci,release}.yml
docs/deploy.md
```

## Status of this phase (F4 + F5)

Implemented and covered by mocked tests: everything from F1–F3 (credentials loading, the CSRF login
handshake, cookie persistence, orders, deliveries, day menus, switch options, the guarded
single-meal switch, the five read-only MCP tools, the three write tools with their audit log and the
shared `dietly_mcp.operations` policy) **plus**:

- **F4 — HTTP transport**: `--transport stdio|streamable-http` + `DIETLY_TRANSPORT`,
  `--host`/`--port` + `DIETLY_HTTP_HOST`/`DIETLY_HTTP_PORT`, a bearer-token gate that refuses
  non-loopback binds without `MCP_AUTH_TOKEN`, a credential-free public `GET /healthz` that discloses
  booleans only, stdout logging, concurrency-safe client/session handling and serialised writes, and
  `DIETLY_CONFIG_DIR`/`DIETLY_STATE_DIR` for containers;
- **F5 — container + CI/CD**: a multi-stage non-root `Dockerfile` (uv + `uv sync --frozen`, `/data`
  volume, healthcheck, `dietly-mcp-server` entry point), `.dockerignore`, `ci.yml` (lint + tests on
  PR/push) and `release.yml` (tag → verify version, test, cross-build amd64+arm64 with provenance and
  SBOM, push to GHCR, create the GitHub Release).

Verified against the live API on 2026-10-16 (order `3597452`, catering `kuchniavikinga`, diet
"Wybór menu", 2000 kcal):

- a dry run on `2026-10-29` slot `Kolacja` returned the preview (meal `2224` → `3648`) and sent
  nothing — through the CLI **and** through the MCP tools over the in-memory transport;
- one real switch + immediate `revert` on `2026-10-29` left the day **byte-identical** (23362-byte
  JSON snapshot before/after);
- the same switch + revert on the nearest future day `2026-10-26` was also accepted by Dietly and
  left it byte-identical (24073 bytes) — so the real cutoff was *not* reached by this order's
  schedule (its next delivery after the past `2026-10-07` is `2026-10-26`, ten days out);
- both real writes and both reverts, plus the dry runs and the deliberately-invalid probes, are in
  `~/.local/state/dietly-mcp/audit.jsonl`.

Nothing else was written; no other day, slot or order was touched.

The HTTP transport and the container were validated locally against a **dummy** token and dummy
credentials (never the real account): the image starts, `GET /healthz` answers 200 without
credentials, an unauthenticated `POST /mcp` is `401`, and an authenticated SDK client sees the eight
`dietly_*` tools. The exact commands and output are in the mission report; the reusable recipes are in
[docs/deploy.md](docs/deploy.md).

