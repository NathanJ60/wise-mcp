# wise-mcp

[![CI](https://github.com/NathanJ60/wise-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/NathanJ60/wise-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![MCP](https://img.shields.io/badge/MCP-python--sdk%202.x-purple)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Typed](https://img.shields.io/badge/typing-mypy%20strict-informational)

A **secure, scope-gated [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Wise](https://wise.com) API**. It gives an AI assistant (Claude Desktop, Claude Code, Cursor,
Hermes...) access to your Wise account through a deny-by-default permission model:

- **Read-only by default.** With no configuration, the server can only read: profiles, balances and
  jars, bank details (IBAN / sort code / ACH), the activity feed (card payments included),
  statements, transfers, recipients, exchange rates, invoices and webhooks.
- **Writes are opt-in, per capability** (`WISE_ENABLE=invoices,...`). A hard **deny list**
  (`WISE_DENY=transfers,...`) always wins, and `WISE_READ_ONLY=true` turns every write off.
- **Disabled tools are not registered at all**, so the model never sees them.
- **Defense in depth:** the HTTP client enforces the same policy on every request (method + path
  allowlist) *before* any network call, so even a tool registered by mistake cannot reach a denied
  endpoint.
- **Money-moving operations are two-step** (`prepare_*` then `execute_*`) with a single-use,
  5-minute, HMAC-bound confirmation token, optional amount caps and recipient/profile allowlists.
- **Every write attempt is audited** (allowed or blocked) to a local JSONL file, without secrets or
  full account numbers.

> [!WARNING]
> This is an unofficial project, not affiliated with Wise. Enabling write scopes lets an AI agent act
> on a real financial account. Read the [security model](#security-model) before enabling anything.

---

## Contents

- [Quickstart](#quickstart)
- [Client configuration](#client-configuration)
- [Configuration reference](#configuration-reference)
- [Security model](#security-model)
- [Tools](#tools)
- [Creating a Wise API token](#creating-a-wise-api-token)
- [Strong Customer Authentication (SCA)](#strong-customer-authentication-sca)
- [Sandbox](#sandbox)
- [Limitations](#limitations)
- [Development](#development)

## Quickstart

Requires Python 3.12+ and a Wise **personal API token**.

```bash
# Run straight from the repository with uv (no install)
uvx --from git+https://github.com/NathanJ60/wise-mcp wise-mcp --print-policy

# Or from a local clone
git clone https://github.com/NathanJ60/wise-mcp && cd wise-mcp
uv sync
WISE_API_TOKEN=... uv run wise-mcp --print-policy   # show effective permissions, then exit
WISE_API_TOKEN=... uv run wise-mcp                  # start the stdio server

# Or with pip
pip install git+https://github.com/NathanJ60/wise-mcp
wise-mcp --version
```

The server speaks MCP over **stdio** only. It never opens a network port. Logs go to stderr.

## Client configuration

All examples below are read-only. Add `WISE_ENABLE` / `WISE_DENY` to grant more
(see [recommended configurations](#recommended-configurations)).

### Claude Desktop

`claude_desktop_config.json` (Settings > Developer > Edit config):

```json
{
  "mcpServers": {
    "wise": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/NathanJ60/wise-mcp", "wise-mcp"],
      "env": {
        "WISE_API_TOKEN": "your-token",
        "WISE_READ_ONLY": "true"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add wise --scope user \
  --env WISE_API_TOKEN=your-token --env WISE_READ_ONLY=true \
  -- uvx --from git+https://github.com/NathanJ60/wise-mcp wise-mcp
```

### Cursor

`~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "wise": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/NathanJ60/wise-mcp", "wise-mcp"],
      "env": { "WISE_API_TOKEN": "your-token", "WISE_READ_ONLY": "true" }
    }
  }
}
```

### Hermes Agent

```bash
hermes mcp add wise --command uvx \
  --env WISE_API_TOKEN=your-token WISE_READ_ONLY=true \
  --args --from git+https://github.com/NathanJ60/wise-mcp wise-mcp
```

Prefer referencing the token from your secret store or `.env` rather than pasting it into a config
file that might be shared or committed.

## Configuration reference

All configuration comes from environment variables. Invalid values (e.g. an unknown scope name)
stop the server at startup instead of being ignored.

| Variable | Default | Meaning |
|---|---|---|
| `WISE_API_TOKEN` | *(required)* | Wise personal API token. Never logged or returned. |
| `WISE_ENABLE` | *(empty)* | Comma-separated write scopes to enable. `read` is always on. Wildcards are refused. |
| `WISE_DENY` | *(empty)* | Scopes that are hard-blocked. Always wins. Accepts `*` / `writes` for every write scope. |
| `WISE_READ_ONLY` | `false` | `true` forces every write scope off, whatever `WISE_ENABLE` says. |
| `WISE_ALLOWED_PROFILE_IDS` | *(all)* | Comma-separated profile ids the server may touch (path and query are checked). |
| `WISE_ALLOWED_RECIPIENT_IDS` | *(all)* | Recipient ids allowed in quotes, transfers and batch transfers. |
| `WISE_MAX_AMOUNT_PER_TX` | *(none)* | Cap per money movement: `500` (any currency) or `EUR=500,USD=550`. Unlisted currencies are then refused. |
| `WISE_MAX_AMOUNT_PER_DAY` | *(none)* | Same format, summed per UTC day and currency (persisted locally). |
| `WISE_AUDIT_LOG` | `<data dir>/audit.jsonl` | Audit log path. |
| `WISE_DATA_DIR` | `%LOCALAPPDATA%\wise-mcp`, `~/Library/Application Support/wise-mcp` or `~/.local/share/wise-mcp` | Audit log and daily-cap state. |
| `WISE_SCA_PRIVATE_KEY_PATH` | *(none)* | RSA private key (PEM) used to sign SCA challenges. |
| `WISE_SANDBOX` | `false` | `true` uses `https://api.wise-sandbox.com` instead of `https://api.wise.com`. |
| `WISE_TIMEOUT_SECONDS` | `30` | Per-request timeout (1 to 120). |
| `WISE_CONFIRMATION_TTL_SECONDS` | `300` | Confirmation token lifetime (30 to 900). |
| `WISE_LOG_LEVEL` | `WARNING` | Log level (stderr only). |

`wise-mcp --print-policy` (add `--json` for machine output) shows the effective scopes, the tools
that are registered or disabled, the allowlists, the caps and the audit log path. It never prints the
token. The `get_permissions` tool returns the same information to the assistant.

## Security model

### Scopes

| Scope | Default | Moves money? | What it allows |
|---|:-:|:-:|---|
| `read` | **on** | no | Every `GET` tool: profiles, balances, bank details, activities, statements, transfers, recipients, rates, price comparison, invoices, webhooks, batch groups. |
| `invoices` | off | no (receives) | Create and update invoice drafts, publish them, cancel (invalidate) them. |
| `webhooks` | off | no | Create and delete profile webhook subscriptions (HTTPS URLs only). |
| `recipients` | off | no | Create and deactivate recipient (payee) accounts. |
| `quotes` | off | no | Create and update quotes (needed for transfers and conversions). |
| `transfers` | off | **yes** | Validate, create, cancel and **fund** transfers. Denying it also denies `batch_transfers`. |
| `batch_transfers` | off | **yes** | Create, complete, cancel and **fund** batch groups. |
| `balance_moves` | off | **yes** (between your own balances) | Open/close balances and jars, convert currencies, move money between balances/jars. |

Effective scopes are computed once at startup:

```
effective = ({read} ∪ WISE_ENABLE) − WISE_DENY − (all write scopes, if WISE_READ_ONLY=true)
```

`WISE_DENY` cannot be overridden by anything: not by `WISE_ENABLE`, not by a tool argument, not by
the conversation. The server has no tool that changes its own permissions.

### Layers

1. **Registration.** Tools of a disabled scope are never registered with the MCP server: the client
   cannot list or call them.
2. **Request allowlist.** The single HTTP client (`WiseClient`) checks every request against an
   explicit table of `(method, path regex, scope)` routes. Unknown paths, absolute URLs, traversal or
   encoded characters are refused. The deny list is checked first. This happens before any network
   I/O. The test suite proves that with `WISE_DENY=transfers`, no code path can `POST`/`PUT`/`PATCH`
   any transfer, funding or batch endpoint, even with every tool force-registered.
3. **Allowlists.** `WISE_ALLOWED_PROFILE_IDS` is enforced at request level (path and query).
   `WISE_ALLOWED_RECIPIENT_IDS` is enforced for quotes, transfer creation and funding (the
   recipient of the fetched transfer is checked again at funding time).
4. **Two-step confirmation** for operations that move money (`execute_fund_transfer`,
   `execute_fund_batch_group`, `execute_balance_conversion`, `execute_balance_move`):
   - `prepare_*` is read-only. It fetches the real amount from Wise (transfer, batch, quote or
     balances), checks caps and allowlists and returns a human-readable summary plus a
     `confirmation_token`.
   - The token is HMAC-SHA256 signed with a random per-process key, expires after 5 minutes, is
     **single use**, and is bound to the action and the exact parameters (including the amount).
     Changing anything, or calling a different `execute_*`, invalidates it.
   - `execute_*` re-fetches the data, re-checks caps and allowlists, then calls Wise with an
     idempotency key derived from the token.
5. **Amount caps.** `WISE_MAX_AMOUNT_PER_TX` and `WISE_MAX_AMOUNT_PER_DAY` are enforced locally at
   prepare and execute time. When a cap is per currency, other currencies are refused (fail closed).
6. **Explicit `confirm=true`** on other irreversible writes: publishing or cancelling an invoice,
   deleting a webhook, deactivating a recipient, cancelling a transfer, closing a balance,
   completing or cancelling a batch.
7. **Audit log.** Every write attempt is appended to a JSONL file: `write_blocked` (with the reason),
   `write_attempt`, `write_ok` / `write_failed`, `confirmation_issued`. Bodies are sanitized: tokens
   and secrets are removed, IBANs and long digit sequences are masked (`****3000`). If the audit log
   cannot be written, the write is **not** sent.
8. **Transport hygiene.** Fixed base URL, no redirects, timeouts on every request, retries only for
   `GET` on 429/5xx, token only from `WISE_API_TOKEN` and redacted from every error message.
9. **Tool annotations.** Every tool declares `readOnlyHint`, `destructiveHint`, `idempotentHint` and
   `openWorldHint`, so clients that honour them can ask for approval on writes.

Tool annotations and the MCP client's own approval prompts are useful, but they are hints. The
guarantees come from the server-side layers above.

### Recommended configurations

**Read-only (safest):**

```bash
WISE_READ_ONLY=true
```

**Nathan / safe default:** invoices, webhooks, recipients, quotes and moves between your own
balances (with two-step confirmation), but never sending money out:

```bash
WISE_ENABLE=invoices,webhooks,recipients,quotes,balance_moves
WISE_DENY=transfers,batch_transfers
# optional hardening
WISE_ALLOWED_PROFILE_IDS=12345678
WISE_MAX_AMOUNT_PER_TX=EUR=2000,USD=2000,GBP=2000
WISE_MAX_AMOUNT_PER_DAY=EUR=5000,USD=5000,GBP=5000
```

**Invoicing only:**

```bash
WISE_ENABLE=invoices
WISE_DENY=transfers,batch_transfers,balance_moves,recipients,quotes
```

**Full access (only if you really mean it):** enable `transfers` explicitly, and set recipient
allowlists and caps:

```bash
WISE_ENABLE=quotes,transfers
WISE_ALLOWED_RECIPIENT_IDS=111,222
WISE_MAX_AMOUNT_PER_TX=EUR=500
WISE_MAX_AMOUNT_PER_DAY=EUR=1000
```

Also restrict the token itself (see [below](#creating-a-wise-api-token)): a **Read only** Wise token
makes every write fail on Wise's side regardless of this server's configuration.

## Tools

56 tools in total. With the default configuration only the 30 `read` tools are registered.

| Tool | Scope | Wise endpoint | Read-only | Destructive |
|---|---|---|:-:|:-:|
| `get_permissions` | read | local (no API call) | ✅ | |
| `list_profiles` | read | `GET /v2/profiles` | ✅ | |
| `get_profile` | read | `GET /v2/profiles/{profileId}` | ✅ | |
| `get_current_user` | read | `GET /v1/me` | ✅ | |
| `list_balances` | read | `GET /v4/profiles/{profileId}/balances?types=STANDARD,SAVINGS` | ✅ | |
| `get_balance` | read | `GET /v4/profiles/{profileId}/balances/{balanceId}` | ✅ | |
| `get_total_funds` | read | `GET /v1/profiles/{profileId}/total-funds/{currency}` | ✅ | |
| `get_balance_capacity` | read | `GET /v1/profiles/{profileId}/balance-capacity` | ✅ | |
| `get_account_details` | read | `GET /v1/profiles/{profileId}/account-details` | ✅ | |
| `list_account_details_orders` | read | `GET /v3/profiles/{profileId}/account-details-orders` | ✅ | |
| `get_multi_currency_account` | read | `GET /v4/profiles/{profileId}/multi-currency-account` + `GET /v2/borderless-accounts-configuration/profiles/{profileId}/{available,payin}-currencies` | ✅ | |
| `list_activities` | read | `GET /v1/profiles/{profileId}/activities` | ✅ | |
| `get_balance_statement` | read | `GET /v1/profiles/{profileId}/balance-statements/{balanceId}/statement.json` | ✅ | |
| `list_transfers` | read | `GET /v1/transfers` | ✅ | |
| `get_transfer` | read | `GET /v1/transfers/{transferId}` | ✅ | |
| `list_transfer_payments` | read | `GET /v1/transfers/{transferId}/payments` | ✅ | |
| `get_delivery_estimate` | read | `GET /v1/delivery-estimates/{transferId}` | ✅ | |
| `list_recipients` | read | `GET /v1/accounts?profile=` | ✅ | |
| `get_recipient` | read | `GET /v2/accounts/{accountId}` | ✅ | |
| `get_account_requirements` | read | `GET /v1/account-requirements` | ✅ | |
| `get_exchange_rate` | read | `GET /v1/rates` (live or `time=`) | ✅ | |
| `get_rate_history` | read | `GET /v1/rates?from=&to=&group=` | ✅ | |
| `list_currencies` | read | `GET /v1/currencies` | ✅ | |
| `compare_prices` | read | `GET /v4/comparisons` (price estimate, creates nothing) | ✅ | |
| `get_quote` | read | `GET /v3/profiles/{profileId}/quotes/{quoteId}` | ✅ | |
| `list_payment_requests` | read | `GET /v2/profiles/{profileId}/acquiring/payment-requests` | ✅ | |
| `get_payment_request` | read | `GET /v2/profiles/{profileId}/acquiring/payment-requests/{id}` | ✅ | |
| `get_invoice_currency_options` | read | `GET /v1/profiles/{profileId}/acquiring/requesting-configuration/currency-options` | ✅ | |
| `list_webhook_subscriptions` | read | `GET /v3/profiles/{profileId}/subscriptions` | ✅ | |
| `get_batch_group` | read | `GET /v3/profiles/{profileId}/batch-groups/{batchGroupId}` | ✅ | |
| `create_invoice_draft` | invoices | `POST` then `PUT /v2/profiles/{profileId}/acquiring/payment-requests[/{id}]` | | |
| `update_invoice_draft` | invoices | `PUT /v2/profiles/{profileId}/acquiring/payment-requests/{id}` | | ⚠️ |
| `publish_invoice` | invoices | `PUT .../payment-requests/{id}/status` `{"status":"PUBLISHED"}` (`confirm=true`) | | ⚠️ |
| `cancel_invoice` | invoices | `PUT .../payment-requests/{id}/status` `{"status":"INVALIDATED"}` (`confirm=true`) | | ⚠️ |
| `create_webhook_subscription` | webhooks | `POST /v3/profiles/{profileId}/subscriptions` | | |
| `delete_webhook_subscription` | webhooks | `DELETE /v3/profiles/{profileId}/subscriptions/{id}` (`confirm=true`) | | ⚠️ |
| `create_recipient` | recipients | `POST /v1/accounts` | | |
| `delete_recipient` | recipients | `DELETE /v2/accounts/{accountId}` (`confirm=true`) | | ⚠️ |
| `create_quote` | quotes | `POST /v3/profiles/{profileId}/quotes` | | |
| `update_quote` | quotes | `PATCH /v3/profiles/{profileId}/quotes/{quoteId}` | | |
| `validate_transfer_requirements` | transfers | `POST /v1/transfer-requirements` (validation only) | ✅ | |
| `create_transfer` | transfers | `POST /v1/transfers` (unfunded) | | |
| `cancel_transfer` | transfers | `PUT /v1/transfers/{transferId}/cancel` (`confirm=true`) | | ⚠️ |
| `prepare_fund_transfer` | transfers | `GET /v1/transfers/{transferId}` (step 1/2) | ✅ | |
| `execute_fund_transfer` | transfers | `POST /v3/profiles/{profileId}/transfers/{transferId}/payments` (step 2/2) | | ⚠️ |
| `create_batch_group` | batch_transfers | `POST /v3/profiles/{profileId}/batch-groups` | | |
| `add_batch_transfer` | batch_transfers | `POST /v3/profiles/{profileId}/batch-groups/{id}/transfers` | | |
| `update_batch_group_status` | batch_transfers | `PATCH /v3/profiles/{profileId}/batch-groups/{id}` (`confirm=true`) | | ⚠️ |
| `prepare_fund_batch_group` | batch_transfers | `GET` batch group + its transfers (step 1/2) | ✅ | |
| `execute_fund_batch_group` | batch_transfers | `POST /v3/profiles/{profileId}/batch-payments/{id}/payments` (step 2/2) | | ⚠️ |
| `open_balance` | balance_moves | `POST /v4/profiles/{profileId}/balances` | | |
| `close_balance` | balance_moves | `DELETE /v4/profiles/{profileId}/balances/{balanceId}` (`confirm=true`) | | ⚠️ |
| `prepare_balance_conversion` | balance_moves | `GET /v3/profiles/{profileId}/quotes/{quoteId}` (step 1/2) | ✅ | |
| `execute_balance_conversion` | balance_moves | `POST /v2/profiles/{profileId}/balance-movements` `{quoteId}` (step 2/2) | | ⚠️ |
| `prepare_balance_move` | balance_moves | `GET` both balances (step 1/2) | ✅ | |
| `execute_balance_move` | balance_moves | `POST /v2/profiles/{profileId}/balance-movements` (step 2/2) | | ⚠️ |

Notes:

- `get_account_details` separates **ACTIVE** currencies (real, usable bank coordinates) from
  **AVAILABLE** ones (can be opened, numbers masked by Wise, must not be shared).
- `list_activities` is the feed shown in the Wise app: card payments, transfers, conversions,
  deposits, fees. Filter with `monetary_resource_type`, `status`, `since`, `until`; paginate with
  `cursor`.
- Invoices are a business-profile feature. Drafts use `selectedPaymentMethods` `["BANK_TRANSFER"]`
  by default (`PAY_WITH_WISE` is also accepted; `ACCOUNT_DETAILS` is rejected by Wise). A draft is
  never sent to anyone until `publish_invoice`.
- Every tool returns structured JSON (MCP `structuredContent`) with an output schema. List results
  are wrapped as `{"count": n, "items": [...]}`.

## Creating a Wise API token

In the Wise web app: **Settings > Developer tools > API tokens > Add new token**. Personal tokens are
available for personal and business accounts; business accounts may require an admin.

- **Read only:** enough for every `read` tool. Recommended if you do not need writes: Wise itself
  then refuses every write, whatever this server is configured to do.
- **Full access:** required for invoices, webhooks, recipients, quotes, transfers and balance moves.
  Combine it with `WISE_ENABLE` / `WISE_DENY` so the server only exposes what you need.

Treat the token like a password. Store it in your OS keychain, secret manager or a `.env` file that
is never committed (see [.env.example](.env.example)), and revoke it from the same Wise page if it
leaks.

## Strong Customer Authentication (SCA)

Some endpoints (notably balance statements and funding) are SCA-protected for profiles registered
outside the US, AU, NZ, SG, CA and MY (EU/UK PSD2 rules). Wise then answers `403` with a one-time
token in `x-2fa-approval`. To let the server complete the challenge:

```bash
openssl genrsa -out wise-sca-private.pem 2048
openssl rsa -pubout -in wise-sca-private.pem -out wise-sca-public.pem
```

Upload `wise-sca-public.pem` in Wise (**Settings > Developer tools > API tokens > Manage public
keys**) and set `WISE_SCA_PRIVATE_KEY_PATH=/path/to/wise-sca-private.pem`. The server signs the
one-time token (SHA256withRSA) and retries once with `x-2fa-approval` + `X-Signature`. Without a key
you get an explicit error explaining what to do. For statements the challenge only needs to be
completed once every 90 days. See
[Wise: SCA and 2FA](https://docs.wise.com/guides/developer/auth-and-security/sca-and-2fa).

## Sandbox

Create a sandbox account and token on [sandbox.transferwise.tech](https://sandbox.transferwise.tech),
then run with `WISE_SANDBOX=true`. The base URL becomes `https://api.wise-sandbox.com`. The base URL
can only be one of these two values; it cannot be overridden.

## Limitations

What a **personal token** cannot do (checked against the official docs and/or production):

- **Cards** (list, details, card transactions, card controls): `UserToken` (partner OAuth) only;
  production returns `403`. Card *payments* are still visible through `list_activities`.
- **Direct debit accounts**: `UserToken` only; production returns `401`.
- **Funding transfers / batches** from EU/UK profiles may be refused for personal tokens under PSD2
  even with SCA; use the Wise app to fund in that case.
- **Quotes, recipients and webhooks** are documented by Wise as `UserToken` endpoints. They are
  included because personal tokens are accepted in practice for several of them, but Wise may refuse
  some calls; you will then get the Wise error message.
- **Receipts and statements in PDF/CSV/XLSX** and other binary downloads are not exposed (JSON only).
- **Payment request list filters**: Wise does not publicly document the acquiring API; `status` is
  passed through on a best-effort basis.
- Account opening, KYC, partner/OAuth flows, card ordering and spend limits are out of scope.

## Development

```bash
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Tests never touch the network: Wise is mocked with [respx](https://lundberg.github.io/respx/), tools
are exercised through the official in-process MCP client, and one test spawns the real stdio server.
Dependencies are pinned in `uv.lock`. Runtime dependencies: `mcp`, `httpx`, `pydantic` and
`cryptography` (SCA signing). No telemetry: the only host the server talks to is the Wise API.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © Nathan Jacas
