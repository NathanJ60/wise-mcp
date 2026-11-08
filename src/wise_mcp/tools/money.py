"""Helpers for money-moving tools: two-step confirmation, caps and auditing of local refusals."""

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from wise_mcp.errors import ConfirmationError, LimitError, PolicyError
from wise_mcp.tools.common import App


def to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def prepare(
    app: App,
    tool: str,
    action: str,
    bound: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    currency: str,
    amount: Decimal,
) -> dict[str, Any]:
    """Check caps, issue a confirmation token and return what the user must review."""
    try:
        app.limiter.check(currency, amount)
    except LimitError as exc:
        app.audit.record("write_blocked", tool=tool, reason=str(exc), currency=currency, amount=str(amount))
        raise
    token, expires = app.confirmations.issue(action, bound)
    app.audit.record("confirmation_issued", tool=tool, action=action, currency=currency, amount=str(amount))
    return {
        "summary": dict(summary),
        "amount": str(amount),
        "currency": currency,
        "confirmation_token": token,
        "expires_at_epoch": expires,
        "next_step": f"Show this summary to the user. Only after explicit approval, call "
        f"{action} with the same arguments and this confirmation_token (single use, "
        f"expires in {app.confirmations.ttl}s).",
    }


def consume(
    app: App, tool: str, action: str, token: str, bound: Mapping[str, Any], *, currency: str, amount: Decimal
) -> str:
    """Verify the token and the caps again. Returns a deterministic idempotency key."""
    try:
        nonce = app.confirmations.consume(token, action, bound)
        app.limiter.check(currency, amount)
    except (ConfirmationError, LimitError, PolicyError) as exc:
        app.audit.record("write_blocked", tool=tool, reason=str(exc), currency=currency, amount=str(amount))
        raise
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wise-mcp:{action}:{nonce}"))


def require_confirm(app: App, tool: str, confirm: bool, what: str) -> None:
    if not confirm:
        app.audit.record("write_blocked", tool=tool, reason="confirm=false")
        raise ConfirmationError(f"{what} Re-run with confirm=true once the user has explicitly approved.")
