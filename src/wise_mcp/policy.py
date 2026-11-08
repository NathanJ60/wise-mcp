"""Permission model: capability scopes and the request-level allowlist.

Every HTTP call made by this server goes through :meth:`Policy.authorize` *before* any byte
leaves the process. The rules, in order:

1. The path must be syntactically safe (no traversal, no encoded characters, no query string).
2. The (method, path) pair must match an entry in :data:`ROUTES`. Anything else is refused.
3. If the route's scope is in the deny set, the call is refused. Deny always wins.
4. If the route's scope is not in the effective (enabled) set, the call is refused.
5. If a profile allowlist is configured, the profile id in the path or query must be on it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from wise_mcp.errors import PolicyError


class Scope(StrEnum):
    READ = "read"
    INVOICES = "invoices"
    WEBHOOKS = "webhooks"
    RECIPIENTS = "recipients"
    QUOTES = "quotes"
    TRANSFERS = "transfers"
    BATCH_TRANSFERS = "batch_transfers"
    BALANCE_MOVES = "balance_moves"


WRITE_SCOPES: frozenset[Scope] = frozenset(s for s in Scope if s is not Scope.READ)

#: Scopes whose tools can move money out of (or between) balances.
MONEY_MOVING_SCOPES: frozenset[Scope] = frozenset({Scope.TRANSFERS, Scope.BATCH_TRANSFERS, Scope.BALANCE_MOVES})

#: Denying a scope also denies the scopes listed here (batch transfers *are* transfers).
IMPLIED_DENIES: Mapping[Scope, frozenset[Scope]] = {
    Scope.TRANSFERS: frozenset({Scope.BATCH_TRANSFERS}),
}

SCOPE_DESCRIPTIONS: Mapping[Scope, str] = {
    Scope.READ: "Read-only GET access: profiles, balances, account details, activities, statements, "
    "transfers, recipients, rates, invoices, webhooks.",
    Scope.INVOICES: "Create, fill and publish invoices (acquiring payment requests). Receives money only.",
    Scope.WEBHOOKS: "Create and delete profile webhook subscriptions.",
    Scope.RECIPIENTS: "Create and deactivate recipient (payee) accounts.",
    Scope.QUOTES: "Create and update transfer quotes.",
    Scope.TRANSFERS: "Create, validate, cancel and FUND transfers (moves money out). Also implies "
    "batch_transfers when denied.",
    Scope.BATCH_TRANSFERS: "Create, complete, cancel and FUND batch transfer groups (moves money out).",
    Scope.BALANCE_MOVES: "Open/close balances and jars, convert between currencies, move money between "
    "your own balances.",
}

_ID = r"\d{1,20}"
_UUID = r"[A-Za-z0-9-]{1,64}"
_CUR = r"[A-Z]{3}"
_P = rf"(?P<profile>{_ID})"


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    scope: Scope
    pattern: re.Pattern[str] = field(compare=False, repr=False)


def _r(method: str, template: str, scope: Scope) -> Route:
    regex = template.replace("{p}", _P).replace("{id}", _ID).replace("{uuid}", _UUID).replace("{cur}", _CUR)
    return Route(method, template, scope, re.compile(regex))


R, INV, W, RC, Q, T, B, BM = (
    Scope.READ,
    Scope.INVOICES,
    Scope.WEBHOOKS,
    Scope.RECIPIENTS,
    Scope.QUOTES,
    Scope.TRANSFERS,
    Scope.BATCH_TRANSFERS,
    Scope.BALANCE_MOVES,
)

#: The complete allowlist. Any (method, path) not listed here can never be called.
ROUTES: tuple[Route, ...] = (
    # --- read -------------------------------------------------------------------------------
    _r("GET", "/v2/profiles", R),
    _r("GET", "/v2/profiles/{p}", R),
    _r("GET", "/v1/me", R),
    _r("GET", "/v4/profiles/{p}/balances", R),
    _r("GET", "/v4/profiles/{p}/balances/{id}", R),
    _r("GET", "/v1/profiles/{p}/total-funds/{cur}", R),
    _r("GET", "/v1/profiles/{p}/balance-capacity", R),
    _r("GET", "/v1/profiles/{p}/account-details", R),
    _r("GET", "/v3/profiles/{p}/account-details-orders", R),
    _r("GET", "/v4/profiles/{p}/multi-currency-account", R),
    _r("GET", "/v2/borderless-accounts-configuration/profiles/{p}/available-currencies", R),
    _r("GET", "/v2/borderless-accounts-configuration/profiles/{p}/payin-currencies", R),
    _r("GET", "/v1/profiles/{p}/activities", R),
    _r("GET", "/v1/profiles/{p}/balance-statements/{id}/statement.json", R),
    _r("GET", "/v1/transfers", R),
    _r("GET", "/v1/transfers/{id}", R),
    _r("GET", "/v1/transfers/{id}/payments", R),
    _r("GET", "/v1/delivery-estimates/{id}", R),
    _r("GET", "/v1/accounts", R),
    _r("GET", "/v2/accounts/{id}", R),
    _r("GET", "/v1/account-requirements", R),
    _r("GET", "/v1/rates", R),
    _r("GET", "/v1/currencies", R),
    _r("GET", "/v4/comparisons", R),
    _r("GET", "/v3/profiles/{p}/quotes/{uuid}", R),
    _r("GET", "/v2/profiles/{p}/acquiring/payment-requests", R),
    _r("GET", "/v2/profiles/{p}/acquiring/payment-requests/{uuid}", R),
    _r("GET", "/v1/profiles/{p}/acquiring/requesting-configuration/currency-options", R),
    _r("GET", "/v3/profiles/{p}/subscriptions", R),
    _r("GET", "/v3/profiles/{p}/subscriptions/{uuid}", R),
    _r("GET", "/v3/profiles/{p}/batch-groups/{uuid}", R),
    # --- invoices ---------------------------------------------------------------------------
    _r("POST", "/v2/profiles/{p}/acquiring/payment-requests", INV),
    _r("PUT", "/v2/profiles/{p}/acquiring/payment-requests/{uuid}", INV),
    _r("PUT", "/v2/profiles/{p}/acquiring/payment-requests/{uuid}/status", INV),
    # --- webhooks ---------------------------------------------------------------------------
    _r("POST", "/v3/profiles/{p}/subscriptions", W),
    _r("DELETE", "/v3/profiles/{p}/subscriptions/{uuid}", W),
    # --- recipients -------------------------------------------------------------------------
    _r("POST", "/v1/accounts", RC),
    _r("DELETE", "/v2/accounts/{id}", RC),
    # --- quotes -----------------------------------------------------------------------------
    _r("POST", "/v3/profiles/{p}/quotes", Q),
    _r("PATCH", "/v3/profiles/{p}/quotes/{uuid}", Q),
    # --- transfers --------------------------------------------------------------------------
    _r("POST", "/v1/transfer-requirements", T),
    _r("POST", "/v1/transfers", T),
    _r("PUT", "/v1/transfers/{id}/cancel", T),
    _r("POST", "/v3/profiles/{p}/transfers/{id}/payments", T),
    # --- batch transfers --------------------------------------------------------------------
    _r("POST", "/v3/profiles/{p}/batch-groups", B),
    _r("POST", "/v3/profiles/{p}/batch-groups/{uuid}/transfers", B),
    _r("PATCH", "/v3/profiles/{p}/batch-groups/{uuid}", B),
    _r("POST", "/v3/profiles/{p}/batch-payments/{uuid}/payments", B),
    # --- balance moves ----------------------------------------------------------------------
    _r("POST", "/v4/profiles/{p}/balances", BM),
    _r("DELETE", "/v4/profiles/{p}/balances/{id}", BM),
    _r("POST", "/v2/profiles/{p}/balance-movements", BM),
)

_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_.\-]*$")
_PROFILE_QUERY_KEYS = ("profile", "profileId")


def parse_scopes(raw: str | None, *, env_name: str, allow_wildcards: bool = False) -> frozenset[Scope]:
    """Parse a comma-separated scope list. Unknown names are a hard configuration error.

    Wildcards (``*``, ``all``, ``writes``) are only accepted where ``allow_wildcards`` is set,
    i.e. for WISE_DENY. Enabling must always be explicit, scope by scope.
    """
    if not raw:
        return frozenset()
    out: set[Scope] = set()
    valid = {s.value for s in Scope}
    for item in raw.replace(";", ",").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if name in ("*", "all", "writes"):
            if not allow_wildcards:
                raise ValueError(f"{env_name}: wildcards are not allowed here; list scopes explicitly")
            out |= WRITE_SCOPES
            continue
        if name not in valid:
            raise ValueError(f"{env_name}: unknown scope {name!r}. Valid scopes: {', '.join(sorted(valid))}")
        out.add(Scope(name))
    return frozenset(out)


class Policy:
    """Effective permissions computed from WISE_ENABLE / WISE_DENY / WISE_READ_ONLY."""

    def __init__(
        self,
        enable: Iterable[Scope] = (),
        deny: Iterable[Scope] = (),
        *,
        read_only: bool = False,
        allowed_profile_ids: Iterable[int] | None = None,
    ) -> None:
        denied = set(deny)
        for scope in list(denied):
            denied |= IMPLIED_DENIES.get(scope, frozenset())
        if read_only:
            denied |= WRITE_SCOPES
        requested = {Scope.READ, *enable}
        self.requested: frozenset[Scope] = frozenset(requested)
        self.denied: frozenset[Scope] = frozenset(denied)
        self.read_only = read_only
        self.effective: frozenset[Scope] = frozenset(requested - denied)
        self.allowed_profile_ids: frozenset[int] | None = (
            frozenset(allowed_profile_ids) if allowed_profile_ids is not None else None
        )

    # ------------------------------------------------------------------------------------------
    def is_enabled(self, scope: Scope) -> bool:
        return scope in self.effective and scope not in self.denied

    def check_profile(self, profile_id: int | str | None) -> None:
        if self.allowed_profile_ids is None or profile_id is None:
            return
        try:
            pid = int(profile_id)
        except (TypeError, ValueError):
            raise PolicyError(f"Invalid profile id {profile_id!r}") from None
        if pid not in self.allowed_profile_ids:
            raise PolicyError(f"Profile {pid} is not in WISE_ALLOWED_PROFILE_IDS")

    def match(self, method: str, path: str) -> Route | None:
        m = method.upper()
        for route in ROUTES:
            if route.method == m and route.pattern.fullmatch(path):
                return route
        return None

    def authorize(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> Route:
        """Return the matching route or raise :class:`PolicyError`. Never touches the network."""
        m = method.upper()
        if not _SAFE_PATH.fullmatch(path) or ".." in path or "//" in path:
            raise PolicyError(f"Refused unsafe path {path!r}")
        route = self.match(m, path)
        if route is None:
            raise PolicyError(f"{m} {path} is not on the allowlist")
        # Deny list first: it is absolute.
        if route.scope in self.denied:
            raise PolicyError(
                f"{m} {route.template} belongs to scope '{route.scope}', which is hard-denied "
                "(WISE_DENY / WISE_READ_ONLY)"
            )
        if route.scope not in self.effective:
            raise PolicyError(
                f"{m} {route.template} belongs to scope '{route.scope}', which is not enabled (add it to WISE_ENABLE)"
            )
        match = route.pattern.fullmatch(path)
        if match and "profile" in match.groupdict():
            self.check_profile(match.group("profile"))
        for key in _PROFILE_QUERY_KEYS:
            if params and params.get(key) is not None:
                self.check_profile(params[key])
        return route

    # ------------------------------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        scopes = []
        for scope in Scope:
            if scope in self.denied:
                state = "DENIED"
            elif scope in self.effective:
                state = "enabled"
            else:
                state = "off"
            scopes.append(
                {
                    "scope": scope.value,
                    "state": state,
                    "money_moving": scope in MONEY_MOVING_SCOPES,
                    "description": SCOPE_DESCRIPTIONS[scope],
                    "endpoints": [f"{r.method} {r.template}" for r in ROUTES if r.scope is scope],
                }
            )
        return {
            "read_only_mode": self.read_only,
            "effective_scopes": sorted(s.value for s in self.effective),
            "denied_scopes": sorted(s.value for s in self.denied),
            "allowed_profile_ids": sorted(self.allowed_profile_ids) if self.allowed_profile_ids is not None else None,
            "scopes": scopes,
        }
