from __future__ import annotations

from decimal import Decimal

import pytest

from wise_mcp.config import Settings, parse_caps
from wise_mcp.confirm import ConfirmationManager
from wise_mcp.errors import ConfigError, ConfirmationError, LimitError
from wise_mcp.limits import SpendLimiter
from wise_mcp.policy import Scope
from wise_mcp.redact import redact_text, sanitize


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def test_confirmation_happy_path_and_single_use() -> None:
    m = ConfirmationManager(300)
    token, _ = m.issue("execute_x", {"a": 1, "b": "2"})
    m.consume(token, "execute_x", {"b": "2", "a": 1})
    with pytest.raises(ConfirmationError, match="already used"):
        m.consume(token, "execute_x", {"a": 1, "b": "2"})


def test_confirmation_rejects_param_change_and_burns_token() -> None:
    m = ConfirmationManager(300)
    token, _ = m.issue("execute_x", {"amount": "10"})
    with pytest.raises(ConfirmationError, match="differ"):
        m.consume(token, "execute_x", {"amount": "11"})
    with pytest.raises(ConfirmationError, match="already used"):
        m.consume(token, "execute_x", {"amount": "10"})


def test_confirmation_expiry_action_and_forgery() -> None:
    clock = Clock()
    m = ConfirmationManager(300, clock=clock)
    token, _ = m.issue("execute_x", {})
    clock.t += 301
    with pytest.raises(ConfirmationError, match="expired"):
        m.consume(token, "execute_x", {})
    token, _ = m.issue("execute_x", {})
    with pytest.raises(ConfirmationError, match="different action"):
        m.consume(token, "execute_y", {})
    other = ConfirmationManager(300)
    forged, _ = other.issue("execute_x", {})
    with pytest.raises(ConfirmationError, match="Invalid"):
        m.consume(forged, "execute_x", {})
    with pytest.raises(ConfirmationError, match="Malformed"):
        m.consume("garbage", "execute_x", {})


def test_caps_parsing() -> None:
    assert parse_caps("500", "X") == {"*": Decimal("500")}
    assert parse_caps("eur=500, USD:600", "X") == {"EUR": Decimal("500"), "USD": Decimal("600")}
    assert parse_caps(None, "X") is None
    with pytest.raises(ConfigError):
        parse_caps("EURO=5", "X")
    with pytest.raises(ConfigError):
        parse_caps("abc", "X")


def test_limiter_per_tx_per_day_and_fail_closed(tmp_path) -> None:
    lim = SpendLimiter({"EUR": Decimal("100")}, {"EUR": Decimal("150")}, tmp_path / "s.json")
    lim.check("EUR", Decimal("100"))
    with pytest.raises(LimitError, match="PER_TX"):
        lim.check("EUR", Decimal("100.01"))
    with pytest.raises(LimitError, match="fail closed"):
        lim.check("USD", Decimal("1"))
    lim.record("EUR", Decimal("100"))
    with pytest.raises(LimitError, match="PER_DAY"):
        lim.check("EUR", Decimal("60"))
    lim.check("EUR", Decimal("50"))
    assert SpendLimiter(None, {"*": Decimal("150")}, tmp_path / "s.json").spent_today("EUR") == Decimal("100")


def test_settings_validation(tmp_path) -> None:
    with pytest.raises(ConfigError, match="unknown scope"):
        Settings.from_env({"WISE_ENABLE": "transfer"})
    with pytest.raises(ConfigError, match="wildcards"):
        Settings.from_env({"WISE_ENABLE": "all"})
    with pytest.raises(ConfigError):
        Settings.from_env({"WISE_READ_ONLY": "maybe"})
    with pytest.raises(ConfigError):
        Settings.from_env({"WISE_ALLOWED_PROFILE_IDS": "1,abc"})
    s = Settings.from_env({"WISE_API_TOKEN": "tok", "WISE_DENY": "*", "WISE_DATA_DIR": str(tmp_path)})
    assert s.policy().effective == {Scope.READ}
    assert "tok" not in repr(s)


def test_redaction() -> None:
    assert "1234567890" not in redact_text("acct 1234567890")
    assert redact_text("acct 1234567890").endswith("7890")
    assert "DE89370400440532013000" not in redact_text("iban DE89370400440532013000")
    assert redact_text("my secret-tok here", ["secret-tok"]) == "my *** here"
    out = sanitize({"details": {"accountNumber": "12345678", "sortCode": "040004", "name": "Bob"}})
    assert out["details"]["accountNumber"] == "****5678"
    assert out["details"]["name"] == "Bob"
