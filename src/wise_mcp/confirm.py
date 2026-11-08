"""Two-step confirmation tokens for money-moving operations.

``issue()`` returns a short-lived, single-use token bound (via HMAC-SHA256) to an action name
and the exact canonical parameters. ``consume()`` verifies and burns it. The HMAC key is random
per server process, so tokens never survive a restart and cannot be forged by the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from wise_mcp.errors import ConfirmationError


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def params_digest(params: Mapping[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ConfirmationManager:
    def __init__(
        self,
        ttl_seconds: int = 300,
        *,
        key: bytes | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl = ttl_seconds
        self._key = key or secrets.token_bytes(32)
        self._clock = clock
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest()[:24])

    def issue(self, action: str, params: Mapping[str, Any]) -> tuple[str, int]:
        expires = int(self._clock()) + self.ttl
        payload = {"a": action, "n": secrets.token_hex(8), "e": expires, "h": params_digest(params)}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._mac(body)}", expires

    def consume(self, token: str, action: str, params: Mapping[str, Any]) -> str:
        """Verify and burn ``token``. Returns the token nonce (usable as an idempotency seed)."""
        try:
            body, mac = token.strip().split(".", 1)
        except (ValueError, AttributeError):
            raise ConfirmationError("Malformed confirmation token") from None
        if not hmac.compare_digest(mac, self._mac(body)):
            raise ConfirmationError("Invalid confirmation token (not issued by this server process)")
        try:
            payload = json.loads(_unb64(body))
        except ValueError:
            raise ConfirmationError("Malformed confirmation token") from None
        nonce = str(payload["n"])
        now = self._clock()
        with self._lock:
            for n, exp in list(self._used.items()):
                if exp < now:
                    del self._used[n]
            if nonce in self._used:
                raise ConfirmationError("Confirmation token already used; run the prepare step again")
            # Burn the token on any authentic attempt, even if it then fails a check below.
            self._used[nonce] = float(payload["e"]) + 1
        if payload["a"] != action:
            raise ConfirmationError("Confirmation token was issued for a different action")
        if now > float(payload["e"]):
            raise ConfirmationError("Confirmation token expired; run the prepare step again")
        if not hmac.compare_digest(str(payload["h"]), params_digest(params)):
            raise ConfirmationError("Parameters differ from the ones that were prepared/confirmed")
        return nonce
