"""Configuration, loaded exclusively from environment variables."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from wise_mcp.errors import ConfigError
from wise_mcp.policy import Policy, Scope, parse_scopes

PRODUCTION_BASE_URL = "https://api.wise.com"
SANDBOX_BASE_URL = "https://api.wise-sandbox.com"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}: expected true/false, got {raw!r}")


def _id_set(env: Mapping[str, str], name: str) -> frozenset[int] | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())
    except ValueError:
        raise ConfigError(f"{name}: expected a comma-separated list of integer ids") from None


def parse_caps(raw: str | None, name: str) -> dict[str, Decimal] | None:
    """Parse an amount cap.

    ``"500"`` applies to every currency. ``"EUR=500,USD=550"`` is per currency; currencies not
    listed are then refused (fail closed) unless a ``*=...`` entry is present.
    """
    if raw is None or not raw.strip():
        return None
    caps: dict[str, Decimal] = {}
    try:
        if "=" not in raw and ":" not in raw:
            caps["*"] = Decimal(raw.strip())
        else:
            for part in raw.split(","):
                if not part.strip():
                    continue
                cur, _, amount = part.replace(":", "=").partition("=")
                key = cur.strip().upper()
                if key != "*" and (len(key) != 3 or not key.isalpha()):
                    raise ConfigError(f"{name}: invalid currency code {cur!r}")
                caps[key] = Decimal(amount.strip())
    except InvalidOperation:
        raise ConfigError(f"{name}: invalid amount in {raw!r}") from None
    if any(v < 0 for v in caps.values()):
        raise ConfigError(f"{name}: amounts must be >= 0")
    return caps


def default_data_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "wise-mcp"


class _Secret(str):
    """A str whose repr never reveals the value."""

    def __repr__(self) -> str:
        return "'***'"


@dataclass(frozen=True)
class Settings:
    token: str | None = field(default=None, repr=False)
    sandbox: bool = False
    enable: frozenset[Scope] = frozenset()
    deny: frozenset[Scope] = frozenset()
    read_only: bool = False
    allowed_profile_ids: frozenset[int] | None = None
    allowed_recipient_ids: frozenset[int] | None = None
    max_amount_per_tx: dict[str, Decimal] | None = None
    max_amount_per_day: dict[str, Decimal] | None = None
    data_dir: Path = field(default_factory=default_data_dir)
    audit_log_path: Path | None = None
    sca_private_key_path: Path | None = None
    timeout_seconds: float = 30.0
    confirmation_ttl_seconds: int = 300

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE_URL if self.sandbox else PRODUCTION_BASE_URL

    @property
    def audit_path(self) -> Path:
        return self.audit_log_path or (self.data_dir / "audit.jsonl")

    @property
    def spend_state_path(self) -> Path:
        return self.data_dir / "daily-spend.json"

    def policy(self) -> Policy:
        return Policy(
            self.enable,
            self.deny,
            read_only=self.read_only,
            allowed_profile_ids=self.allowed_profile_ids,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        try:
            enable = parse_scopes(env.get("WISE_ENABLE"), env_name="WISE_ENABLE")
            deny = parse_scopes(env.get("WISE_DENY"), env_name="WISE_DENY", allow_wildcards=True)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        token = (env.get("WISE_API_TOKEN") or "").strip() or None
        try:
            timeout = float(env.get("WISE_TIMEOUT_SECONDS", "30"))
            ttl = int(env.get("WISE_CONFIRMATION_TTL_SECONDS", "300"))
        except ValueError:
            raise ConfigError("WISE_TIMEOUT_SECONDS / WISE_CONFIRMATION_TTL_SECONDS must be numbers") from None
        if not 1 <= timeout <= 120:
            raise ConfigError("WISE_TIMEOUT_SECONDS must be between 1 and 120")
        if not 30 <= ttl <= 900:
            raise ConfigError("WISE_CONFIRMATION_TTL_SECONDS must be between 30 and 900")
        data_dir = Path(env["WISE_DATA_DIR"]) if env.get("WISE_DATA_DIR") else default_data_dir(env)
        audit = Path(env["WISE_AUDIT_LOG"]) if env.get("WISE_AUDIT_LOG") else None
        key = Path(env["WISE_SCA_PRIVATE_KEY_PATH"]) if env.get("WISE_SCA_PRIVATE_KEY_PATH") else None
        return cls(
            token=_Secret(token) if token else None,
            sandbox=_bool(env, "WISE_SANDBOX"),
            enable=enable,
            deny=deny,
            read_only=_bool(env, "WISE_READ_ONLY"),
            allowed_profile_ids=_id_set(env, "WISE_ALLOWED_PROFILE_IDS"),
            allowed_recipient_ids=_id_set(env, "WISE_ALLOWED_RECIPIENT_IDS"),
            max_amount_per_tx=parse_caps(env.get("WISE_MAX_AMOUNT_PER_TX"), "WISE_MAX_AMOUNT_PER_TX"),
            max_amount_per_day=parse_caps(env.get("WISE_MAX_AMOUNT_PER_DAY"), "WISE_MAX_AMOUNT_PER_DAY"),
            data_dir=data_dir,
            audit_log_path=audit,
            sca_private_key_path=key,
            timeout_seconds=timeout,
            confirmation_ttl_seconds=ttl,
        )
