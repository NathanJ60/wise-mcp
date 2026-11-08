"""Append-only JSONL audit log of every write attempt (allowed or blocked)."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wise_mcp.errors import WiseMcpError
from wise_mcp.redact import sanitize


class AuditError(WiseMcpError):
    """The audit log could not be written. Write operations fail closed in that case."""


class AuditLog:
    def __init__(self, path: Path, secrets: tuple[str | None, ...] = ()) -> None:
        self.path = path
        self._secrets = secrets
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            "pid": os.getpid(),
        }
        entry.update(sanitize(fields, self._secrets))
        line = json.dumps(entry, ensure_ascii=False, default=str, separators=(",", ":"))
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            raise AuditError(f"Cannot write audit log at {self.path}: {exc.strerror}") from None
