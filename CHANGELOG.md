# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-18

### Added

- **Full nutrition (macros) in every meal payload.** Beside the top-level `kcal`, read and write
  tools (`dietly_day`, `dietly_options`, `dietly_switch`, `dietly_apply`) now carry a compact
  `macros` object (protein, carbs, fat, fiber, sugar, salt, saturates in grams, plus `weight` and
  `kcalText`) — on both the `from` and `to` sides of a switch, in the preview and the result.
  `dietly_options` exposes it on its top-level `current` summary too.

### Changed

- **CLI macro parity.** `dietly-mcp day`, `options` and the `switch` plan print the same compact
  macro figures as the MCP tools (`--json` keeps the raw `nutrition` dict).

## [0.1.0] - 2026-10-16

### Added

- **Dietly API client** with the Spring CSRF login handshake (priming `GET` → `XSRF-TOKEN` cookie →
  `POST` with `X-XSRF-TOKEN`), cookie-jar persistence (`0600`), IPv4-pinned transport, explicit
  timeouts and bounded retries.
- **CLI** (`dietly-mcp`) over the client: `login`, `status`, `orders`, `deliveries`, `day`,
  `options`, `switch`, `revert`, `audit`, `version` — with `--json` throughout.
- **MCP server** (`dietly-mcp-server`) over **stdio**, exposing five read-only tools
  (`dietly_status`, `dietly_orders`, `dietly_deliveries`, `dietly_day`, `dietly_options`).
- **Guarded write tools** (`dietly_switch`, `dietly_apply`, `dietly_revert`): dry-run by default,
  target validation against the offered options, an append-only audit log and revert from an audit
  record — sharing one write path with the CLI.
- **Streamable HTTP transport**: `--transport streamable-http` with `--host`/`--port`, a bearer-token
  gate (`MCP_AUTH_TOKEN`) that refuses non-loopback binds without a token, a public `GET /healthz`
  that discloses booleans only, and stdout logging.
- **Container image** (`Dockerfile`): multi-stage, non-root, `uv sync --frozen`, `/data` volume,
  healthcheck.
- **CI/CD**: `ci.yml` (lint + tests on PR/push) and `release.yml` (tag → verify version, test,
  cross-build `linux/amd64` + `linux/arm64` with provenance and SBOM, push to GHCR, create the
  GitHub Release).

[0.2.0]: https://github.com/psampir/dietly-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/psampir/dietly-mcp/releases/tag/v0.1.0
