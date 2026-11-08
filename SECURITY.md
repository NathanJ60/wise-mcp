# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
[GitHub security advisories](https://github.com/NathanJ60/wise-mcp/security/advisories/new) or by
email to jacas.nathan@gmail.com. Include steps to reproduce and the affected version. You should get
an answer within 7 days.

Never include a real Wise API token, account number or statement in a report.

## Scope

In scope: anything that lets this server perform an action that its configured policy should
prevent, for example:

- reaching an endpoint of a disabled or denied scope (`WISE_ENABLE`, `WISE_DENY`, `WISE_READ_ONLY`);
- bypassing the profile or recipient allowlists or the amount caps;
- executing a money-moving operation without a valid, matching, unexpired, unused confirmation token;
- leaking the API token or full bank account numbers through tool output, errors, logs or the audit log;
- making the server send requests to a host other than the Wise API.

Out of scope: behaviour of the Wise API itself, MCP clients, or a compromised local machine (anyone
who can change the server's environment variables already controls its permissions).

## Design summary

See the [Security model](README.md#security-model) section of the README. Key points:

- Read-only by default; write capabilities are explicit opt-in scopes; the deny list always wins.
- Disabled tools are not registered; the HTTP client independently enforces a method + path
  allowlist per scope before any network call.
- Money movements require a two-step, HMAC-bound, single-use confirmation token and honour local caps.
- Every write attempt is audited (JSONL, sanitized). Writes fail closed if the audit log is unwritable.
- The token is read only from `WISE_API_TOKEN` and redacted from every message.

## Recommendations for users

- Prefer a **Read only** Wise token unless you need writes.
- Use `WISE_DENY` for scopes you never want, even if you think they are off.
- Set `WISE_ALLOWED_PROFILE_IDS`, and caps/recipient allowlists if you enable money-moving scopes.
- Review the audit log periodically and rotate your token if it may have leaked.
