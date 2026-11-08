# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-08

### Added

- Stdio MCP server built on the official `mcp` Python SDK 2.x (`MCPServer`), console script `wise-mcp`.
- Scope-based permission model: `read` (default), `invoices`, `webhooks`, `recipients`, `quotes`,
  `transfers`, `batch_transfers`, `balance_moves`; `WISE_ENABLE`, `WISE_DENY` (always wins),
  `WISE_READ_ONLY`.
- Request-level allowlist in the HTTP client (method + path per scope, deny checked first).
- 30 read tools: profiles, user, balances and jars, total funds, deposit limits, bank account details
  (ACTIVE vs not opened), account details orders, multi-currency account, activity feed, balance
  statements (JSON, SCA aware), transfers, transfer payments, delivery estimates, recipients, account
  requirements, live and historical rates, currencies, price comparison, quotes, invoices / payment
  requests, invoice currency options, webhook subscriptions, batch groups, effective permissions.
- Invoice tools: create draft, update draft, publish (`confirm=true`), cancel (`confirm=true`).
- Webhook, recipient and quote write tools.
- Transfer, batch transfer and balance move tools with two-step `prepare_*` / `execute_*`
  confirmation (HMAC, single use, 5 minutes, parameter-bound).
- Optional per-transaction and per-day amount caps, profile and recipient allowlists.
- JSONL audit log of every write attempt (allowed or blocked), sanitized.
- Optional SCA challenge signing with `WISE_SCA_PRIVATE_KEY_PATH`.
- `wise-mcp --print-policy [--json]`.
- Sandbox support (`WISE_SANDBOX=true`).
