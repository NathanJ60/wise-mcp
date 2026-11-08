"""Redaction helpers: keep secrets and full bank account numbers out of logs and audit files."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: Keys whose values are always masked in audit records.
SENSITIVE_KEYS = frozenset(
    k.lower()
    for k in (
        "accountNumber",
        "iban",
        "sortCode",
        "routingNumber",
        "abartn",
        "bsbCode",
        "ifscCode",
        "clabe",
        "cardNumber",
        "pan",
        "cvv",
        "cvc",
        "pin",
        "token",
        "apiToken",
        "authorization",
        "password",
        "secret",
        "x-signature",
        "x-2fa-approval",
        "confirmation_token",
    )
)

_LONG_DIGITS = re.compile(r"\d{7,}")
_IBAN_LIKE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{8,30}\b")


def mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def redact_text(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Remove secret values verbatim and mask anything that looks like an account number."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = _IBAN_LIKE.sub(lambda m: mask(m.group(0)), text)
    return _LONG_DIGITS.sub(lambda m: mask(m.group(0)), text)


def sanitize(obj: Any, secrets: Iterable[str | None] = (), *, _depth: int = 0) -> Any:
    """Return a deep copy of ``obj`` that is safe to persist in the audit log."""
    secrets = tuple(secrets)
    if _depth > 8:
        return "<truncated>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k)
            if key.lower() in SENSITIVE_KEYS:
                out[key] = mask(str(v)) if v not in (None, "") else v
            else:
                out[key] = sanitize(v, secrets, _depth=_depth + 1)
        return out
    if isinstance(obj, list | tuple):
        return [sanitize(v, secrets, _depth=_depth + 1) for v in list(obj)[:50]]
    if isinstance(obj, str):
        return redact_text(obj[:500], secrets)
    return obj
