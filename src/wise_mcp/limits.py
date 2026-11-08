"""Local amount caps (per transaction and per UTC day), enforced before money moves."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from wise_mcp.errors import LimitError


def _cap_for(caps: dict[str, Decimal], currency: str) -> Decimal | None:
    if currency in caps:
        return caps[currency]
    return caps.get("*")


class SpendLimiter:
    def __init__(
        self,
        per_tx: dict[str, Decimal] | None,
        per_day: dict[str, Decimal] | None,
        state_path: Path,
    ) -> None:
        self.per_tx = per_tx
        self.per_day = per_day
        self.state_path = state_path
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def spent_today(self, currency: str) -> Decimal:
        return Decimal(self._load().get(self._today(), {}).get(currency.upper(), "0"))

    def check(self, currency: str, amount: Decimal) -> None:
        cur = currency.upper()
        if amount <= 0:
            raise LimitError("Amount must be positive")
        if self.per_tx is not None:
            cap = _cap_for(self.per_tx, cur)
            if cap is None:
                raise LimitError(f"No WISE_MAX_AMOUNT_PER_TX cap configured for {cur} (fail closed)")
            if amount > cap:
                raise LimitError(f"{amount} {cur} exceeds WISE_MAX_AMOUNT_PER_TX ({cap} {cur})")
        if self.per_day is not None:
            cap = _cap_for(self.per_day, cur)
            if cap is None:
                raise LimitError(f"No WISE_MAX_AMOUNT_PER_DAY cap configured for {cur} (fail closed)")
            spent = self.spent_today(cur)
            if spent + amount > cap:
                raise LimitError(
                    f"{amount} {cur} would exceed WISE_MAX_AMOUNT_PER_DAY ({cap} {cur}; "
                    f"already used today: {spent} {cur})"
                )

    def record(self, currency: str, amount: Decimal) -> None:
        if self.per_day is None:
            return
        cur = currency.upper()
        with self._lock:
            data = self._load()
            today = self._today()
            day = {k: v for k, v in data.get(today, {}).items()}
            day[cur] = str(Decimal(day.get(cur, "0")) + amount)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({today: day}), encoding="utf-8")
