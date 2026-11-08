from __future__ import annotations

import pytest

from wise_mcp.errors import PolicyError
from wise_mcp.policy import (
    MONEY_MOVING_SCOPES,
    ROUTES,
    WRITE_SCOPES,
    Policy,
    Scope,
    parse_scopes,
)


def test_parse_scopes_and_errors() -> None:
    assert parse_scopes("invoices, Webhooks", env_name="X") == {Scope.INVOICES, Scope.WEBHOOKS}
    assert parse_scopes("", env_name="X") == frozenset()
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("invoice", env_name="WISE_ENABLE")
    with pytest.raises(ValueError, match="wildcards"):
        parse_scopes("*", env_name="WISE_ENABLE")
    assert parse_scopes("*", env_name="WISE_DENY", allow_wildcards=True) == WRITE_SCOPES


def test_read_is_the_only_default_scope() -> None:
    assert Policy().effective == {Scope.READ}


def test_deny_always_wins_over_enable() -> None:
    p = Policy(enable=[Scope.TRANSFERS, Scope.INVOICES], deny=[Scope.TRANSFERS])
    assert not p.is_enabled(Scope.TRANSFERS)
    assert p.is_enabled(Scope.INVOICES)


def test_denying_transfers_also_denies_batches() -> None:
    p = Policy(enable=[Scope.BATCH_TRANSFERS], deny=[Scope.TRANSFERS])
    assert Scope.BATCH_TRANSFERS in p.denied
    assert not p.is_enabled(Scope.BATCH_TRANSFERS)


def test_read_only_forces_every_write_scope_off() -> None:
    p = Policy(enable=list(WRITE_SCOPES), read_only=True)
    assert p.effective == {Scope.READ}
    assert p.denied >= WRITE_SCOPES


def test_route_table_invariants() -> None:
    for r in ROUTES:
        if r.method == "GET":
            assert r.scope is Scope.READ, r
        else:
            assert r.scope is not Scope.READ, r
    assert {r.scope for r in ROUTES} == set(Scope)
    assert MONEY_MOVING_SCOPES <= WRITE_SCOPES


@pytest.mark.parametrize("route", [r for r in ROUTES if r.method != "GET"], ids=lambda r: f"{r.method} {r.template}")
def test_every_write_route_is_blocked_when_its_scope_is_denied(route) -> None:  # type: ignore[no-untyped-def]
    path = (
        route.template.replace("{p}", "123").replace("{id}", "456").replace("{uuid}", "abc-def").replace("{cur}", "EUR")
    )
    everything = Policy(enable=list(WRITE_SCOPES))
    assert everything.authorize(route.method, path) is not None
    with pytest.raises(PolicyError, match="hard-denied"):
        Policy(enable=list(WRITE_SCOPES), deny=[route.scope]).authorize(route.method, path)
    with pytest.raises(PolicyError, match="not enabled"):
        Policy().authorize(route.method, path)
    with pytest.raises(PolicyError, match="hard-denied"):
        Policy(enable=list(WRITE_SCOPES), read_only=True).authorize(route.method, path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1/quotes"),  # legacy unscoped quote creation: not allowlisted
        ("POST", "/v1/transfers/1/cancel"),
        ("DELETE", "/v1/transfers/1"),
        ("PUT", "/v4/profiles/1/balances/2"),
        ("POST", "/v3/spend/profiles/1/cards"),
        ("GET", "/v3/spend/profiles/1/cards"),
        ("GET", "/v1/profiles/1/../2/balances"),
        ("GET", "/v1/transfers?x=1"),
        ("GET", "/v1/transfers/%31"),
        ("GET", "//evil.example/v1/me"),
    ],
)
def test_unknown_or_unsafe_requests_are_refused(method: str, path: str) -> None:
    with pytest.raises(PolicyError):
        Policy(enable=list(WRITE_SCOPES)).authorize(method, path)


def test_profile_allowlist_on_path_and_query() -> None:
    p = Policy(allowed_profile_ids=[1])
    p.authorize("GET", "/v4/profiles/1/balances")
    with pytest.raises(PolicyError, match="WISE_ALLOWED_PROFILE_IDS"):
        p.authorize("GET", "/v4/profiles/2/balances")
    with pytest.raises(PolicyError, match="WISE_ALLOWED_PROFILE_IDS"):
        p.authorize("GET", "/v1/transfers", {"profile": 2})
    with pytest.raises(PolicyError, match="WISE_ALLOWED_PROFILE_IDS"):
        p.authorize("GET", "/v1/accounts", {"profileId": "2"})


def test_describe_lists_states() -> None:
    d = Policy(enable=[Scope.INVOICES], deny=[Scope.TRANSFERS]).describe()
    states = {s["scope"]: s["state"] for s in d["scopes"]}
    assert states["read"] == "enabled"
    assert states["invoices"] == "enabled"
    assert states["transfers"] == "DENIED"
    assert states["batch_transfers"] == "DENIED"
    assert states["webhooks"] == "off"
