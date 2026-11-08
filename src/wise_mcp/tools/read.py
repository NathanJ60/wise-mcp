"""Read-only tools (scope: read). GET requests only."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from wise_mcp.errors import WiseMcpError
from wise_mcp.policy import Scope
from wise_mcp.tools.common import (
    BalanceId,
    Currency,
    IsoDateTime,
    ProfileId,
    RecipientId,
    Registrar,
    TransferId,
    Uuid,
    items,
)

ACTIVITY_RESOURCE_TYPES = (
    "ACCRUAL_CHARGE, ACQUIRING_PAYMENT, ASSETS_WITHDRAWAL, BALANCE_CASHBACK, BALANCE_INTEREST, "
    "BALANCE_TRANSACTION, BANK_DETAILS_ORDER, BATCH_TRANSFER, CARD_CASHBACK, CARD_ORDER, "
    "CARD_TRANSACTION, DIRECT_DEBIT_TRANSACTION, TRANSFER, ..."
)
MAX_STATEMENT_DAYS = 469


def _parse_dt(value: str) -> datetime:
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v) if "T" in v else datetime.fromisoformat(v + "T00:00:00+00:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(value: str | None, *, end: bool = False) -> str | None:
    """Normalise a date or date-time into the UTC millisecond format Wise expects."""
    if value is None:
        return None
    if "T" not in value and end:
        value = value + "T23:59:59.999Z"
    return _parse_dt(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def summarize_account_details(data: Any, include_available: bool) -> dict[str, Any]:
    """Split account details into ACTIVE (real coordinates) and not-yet-opened currencies."""
    active: list[dict[str, Any]] = []
    available: list[dict[str, Any]] = []
    for entry in data if isinstance(data, list) else []:
        currency = (entry.get("currency") or {}).get("code")
        status = entry.get("status")
        if status == "ACTIVE":
            active.append(
                {
                    "id": entry.get("id"),
                    "currency": currency,
                    "balanceId": entry.get("balanceId"),
                    "title": entry.get("title"),
                    "status": status,
                    "deprecated": entry.get("deprecated"),
                    "receiveOptions": [
                        {
                            "type": opt.get("type"),
                            "title": opt.get("title"),
                            "details": [
                                {
                                    "title": d.get("title"),
                                    "body": d.get("body"),
                                }
                                for d in (opt.get("details") or [])
                                if isinstance(d, dict)
                            ],
                        }
                        for opt in (entry.get("receiveOptions") or [])
                        if isinstance(opt, dict)
                    ],
                }
            )
        else:
            available.append({"currency": currency, "status": status, "title": entry.get("title")})
    out: dict[str, Any] = {
        "active_count": len(active),
        "active": active,
        "note": "Only ACTIVE entries contain usable bank coordinates. AVAILABLE entries are "
        "currencies that can be opened but are not yet: their numbers are masked by Wise and "
        "must not be shared as payment details.",
    }
    if include_available:
        out["not_opened"] = available
    else:
        out["not_opened_count"] = len(available)
    return out


def register(reg: Registrar) -> None:
    app = reg.app
    c = app.client
    S = Scope.READ

    # --- meta -----------------------------------------------------------------------------
    @reg.tool(S, title="Show effective permissions", read_only=True)
    async def get_permissions() -> dict[str, Any]:
        """Show this server's effective permission policy: enabled/denied scopes, registered tools,
        allowlists and amount caps. Purely local, no API call."""
        d = app.policy.describe()
        d["registered_tools"] = dict(sorted(app.registered_tools.items()))
        d["allowed_recipient_ids"] = (
            sorted(app.settings.allowed_recipient_ids) if app.settings.allowed_recipient_ids else None
        )
        d["max_amount_per_tx"] = {k: str(v) for k, v in (app.settings.max_amount_per_tx or {}).items()}
        d["max_amount_per_day"] = {k: str(v) for k, v in (app.settings.max_amount_per_day or {}).items()}
        d["environment"] = "sandbox" if app.settings.sandbox else "production"
        return d

    # --- profiles -------------------------------------------------------------------------
    @reg.tool(S, title="List profiles", read_only=True)
    async def list_profiles() -> dict[str, Any]:
        """List the Wise profiles (PERSONAL / BUSINESS) the token can access.
        GET /v2/profiles. Filtered by WISE_ALLOWED_PROFILE_IDS when set."""
        data = await c.get("/v2/profiles", tool="list_profiles")
        allowed = app.policy.allowed_profile_ids
        profiles = [
            {
                "id": p.get("id"),
                "type": p.get("type"),
                "fullName": p.get("fullName") or p.get("businessName"),
                "businessName": p.get("businessName"),
                "currentState": p.get("currentState"),
            }
            for p in (data if isinstance(data, list) else [])
            if allowed is None or p.get("id") in allowed
        ]
        return items(profiles)

    @reg.tool(S, title="Get profile", read_only=True)
    async def get_profile(profile_id: ProfileId) -> dict[str, Any]:
        """Get full details of one profile. GET /v2/profiles/{profileId}."""
        result: dict[str, Any] = await c.get(f"/v2/profiles/{profile_id}", tool="get_profile")
        return result

    @reg.tool(S, title="Get current user", read_only=True)
    async def get_current_user() -> dict[str, Any]:
        """Get the user that owns the API token. GET /v1/me."""
        result: dict[str, Any] = await c.get("/v1/me", tool="get_current_user")
        return result

    # --- balances -------------------------------------------------------------------------
    @reg.tool(S, title="List balances", read_only=True)
    async def list_balances(
        profile_id: ProfileId,
        types: Annotated[
            list[Literal["STANDARD", "SAVINGS"]],
            Field(description="STANDARD = currency balances, SAVINGS = jars."),
        ] = ["STANDARD", "SAVINGS"],  # noqa: B006 - immutable in practice, used for the schema default
    ) -> dict[str, Any]:
        """List multi-currency balances and jars. GET /v4/profiles/{profileId}/balances."""
        data = await c.get(f"/v4/profiles/{profile_id}/balances", {"types": ",".join(types)}, tool="list_balances")
        return items(data)

    @reg.tool(S, title="Get balance", read_only=True)
    async def get_balance(profile_id: ProfileId, balance_id: BalanceId) -> dict[str, Any]:
        """Get one balance or jar. GET /v4/profiles/{profileId}/balances/{balanceId}."""
        result: dict[str, Any] = await c.get(f"/v4/profiles/{profile_id}/balances/{balance_id}", tool="get_balance")
        return result

    @reg.tool(S, title="Get total funds", read_only=True)
    async def get_total_funds(profile_id: ProfileId, currency: Currency) -> dict[str, Any]:
        """Total worth / available / cash of all balances, expressed in one currency.
        GET /v1/profiles/{profileId}/total-funds/{currency}."""
        result: dict[str, Any] = await c.get(
            f"/v1/profiles/{profile_id}/total-funds/{currency}", tool="get_total_funds"
        )
        return result

    @reg.tool(S, title="Get balance deposit limit", read_only=True)
    async def get_balance_capacity(profile_id: ProfileId, currency: Currency) -> dict[str, Any]:
        """Regulatory deposit limit for a balance currency, if any.
        GET /v1/profiles/{profileId}/balance-capacity."""
        result: dict[str, Any] = await c.get(
            f"/v1/profiles/{profile_id}/balance-capacity", {"currency": currency}, tool="get_balance_capacity"
        )
        return result

    # --- account details --------------------------------------------------------------------
    @reg.tool(S, title="Get bank account details", read_only=True)
    async def get_account_details(
        profile_id: ProfileId,
        include_not_opened: Annotated[
            bool, Field(description="Also list currencies that could be opened (no usable numbers).")
        ] = False,
    ) -> dict[str, Any]:
        """Bank coordinates (IBAN, sort code, ACH routing/account, SWIFT...) per currency.
        Only ACTIVE entries are real, usable details; AVAILABLE ones are not opened yet.
        GET /v1/profiles/{profileId}/account-details."""
        data = await c.get(f"/v1/profiles/{profile_id}/account-details", tool="get_account_details")
        return summarize_account_details(data, include_not_opened)

    @reg.tool(S, title="List account details orders", read_only=True)
    async def list_account_details_orders(profile_id: ProfileId, currency: Currency) -> dict[str, Any]:
        """Status of requests to open bank details for a currency.
        GET /v3/profiles/{profileId}/account-details-orders."""
        data = await c.get(
            f"/v3/profiles/{profile_id}/account-details-orders",
            {"currency": currency},
            tool="list_account_details_orders",
        )
        return items(data)

    @reg.tool(S, title="Get multi-currency account", read_only=True)
    async def get_multi_currency_account(profile_id: ProfileId) -> dict[str, Any]:
        """Multi-currency account status plus the currencies that can be held and received.
        GET /v4/profiles/{profileId}/multi-currency-account and
        GET /v2/borderless-accounts-configuration/profiles/{profileId}/(available|payin)-currencies."""
        mca = await c.get(f"/v4/profiles/{profile_id}/multi-currency-account", tool="get_multi_currency_account")
        base = f"/v2/borderless-accounts-configuration/profiles/{profile_id}"
        available = await c.get(f"{base}/available-currencies", tool="get_multi_currency_account")
        payin = await c.get(f"{base}/payin-currencies", tool="get_multi_currency_account")
        return {"account": mca, "balance_currencies": available, "payin_currencies": payin}

    # --- activities & statements ----------------------------------------------------------
    @reg.tool(S, title="List activities", read_only=True)
    async def list_activities(
        profile_id: ProfileId,
        monetary_resource_type: Annotated[
            str | None,
            Field(pattern=r"^[A-Z_]{3,40}$", description=f"Filter by type: {ACTIVITY_RESOURCE_TYPES}"),
        ] = None,
        status: Literal["REQUIRES_ATTENTION", "IN_PROGRESS", "UPCOMING", "COMPLETED", "CANCELLED"] | None = None,
        since: IsoDateTime | None = None,
        until: IsoDateTime | None = None,
        cursor: Annotated[str | None, Field(max_length=500, description="`cursor` from a previous page.")] = None,
        size: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Activity feed: card payments, transfers, conversions, incoming payments, fees...
        Paginated with `cursor`. GET /v1/profiles/{profileId}/activities."""
        params = {
            "monetaryResourceType": monetary_resource_type,
            "status": status,
            "since": _iso(since),
            "until": _iso(until, end=True),
            "nextCursor": cursor,
            "size": size,
        }
        data = await c.get(f"/v1/profiles/{profile_id}/activities", params, tool="list_activities")
        acts = data.get("activities", []) if isinstance(data, dict) else []
        return items(acts, next_cursor=data.get("cursor") if isinstance(data, dict) else None)

    @reg.tool(S, title="Get balance statement", read_only=True)
    async def get_balance_statement(
        profile_id: ProfileId,
        balance_id: BalanceId,
        currency: Currency,
        interval_start: IsoDateTime,
        interval_end: IsoDateTime,
        statement_type: Literal["COMPACT", "FLAT"] = "COMPACT",
        locale: Annotated[str, Field(pattern=r"^[a-z]{2}$")] = "en",
    ) -> dict[str, Any]:
        """Balance statement (JSON) for a period of at most 469 days.
        SCA protected for profiles outside US/AU/NZ/SG/CA/MY: set WISE_SCA_PRIVATE_KEY_PATH.
        GET /v1/profiles/{profileId}/balance-statements/{balanceId}/statement.json."""
        start, end = _iso(interval_start), _iso(interval_end, end=True)
        assert start and end
        if (_parse_dt(end) - _parse_dt(start)).days > MAX_STATEMENT_DAYS:
            raise WiseMcpError(f"Statement period cannot exceed {MAX_STATEMENT_DAYS} days")
        if _parse_dt(end) < _parse_dt(start):
            raise WiseMcpError("interval_end must be after interval_start")
        result: dict[str, Any] = await c.get(
            f"/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
            {
                "currency": currency,
                "intervalStart": start,
                "intervalEnd": end,
                "type": statement_type,
                "statementLocale": locale,
            },
            tool="get_balance_statement",
        )
        return result

    # --- transfers ------------------------------------------------------------------------
    @reg.tool(S, title="List transfers", read_only=True)
    async def list_transfers(
        profile_id: ProfileId,
        status: Annotated[
            str | None,
            Field(pattern=r"^[a-z_,]{3,200}$", description="e.g. outgoing_payment_sent, funds_refunded"),
        ] = None,
        created_date_start: IsoDateTime | None = None,
        created_date_end: IsoDateTime | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Transfer history. GET /v1/transfers."""
        data = await c.get(
            "/v1/transfers",
            {
                "profile": profile_id,
                "status": status,
                "createdDateStart": _iso(created_date_start),
                "createdDateEnd": _iso(created_date_end, end=True),
                "limit": limit,
                "offset": offset,
            },
            tool="list_transfers",
        )
        return items(data, limit=limit, offset=offset)

    @reg.tool(S, title="Get transfer", read_only=True)
    async def get_transfer(transfer_id: TransferId) -> dict[str, Any]:
        """Transfer details and status. GET /v1/transfers/{transferId}."""
        result: dict[str, Any] = await c.get(f"/v1/transfers/{transfer_id}", tool="get_transfer")
        return result

    @reg.tool(S, title="List transfer payments", read_only=True)
    async def list_transfer_payments(transfer_id: TransferId) -> dict[str, Any]:
        """Pay-ins recorded for a transfer. GET /v1/transfers/{transferId}/payments."""
        return items(await c.get(f"/v1/transfers/{transfer_id}/payments", tool="list_transfer_payments"))

    @reg.tool(S, title="Get delivery estimate", read_only=True)
    async def get_delivery_estimate(transfer_id: TransferId) -> dict[str, Any]:
        """Estimated delivery date of a transfer. GET /v1/delivery-estimates/{transferId}."""
        result: dict[str, Any] = await c.get(f"/v1/delivery-estimates/{transfer_id}", tool="get_delivery_estimate")
        return result

    # --- recipients -----------------------------------------------------------------------
    @reg.tool(S, title="List recipients", read_only=True)
    async def list_recipients(profile_id: ProfileId, currency: Currency | None = None) -> dict[str, Any]:
        """Saved recipient accounts. GET /v1/accounts?profile=..."""
        data = await c.get("/v1/accounts", {"profile": profile_id, "currency": currency}, tool="list_recipients")
        return items(data)

    @reg.tool(S, title="Get recipient", read_only=True)
    async def get_recipient(recipient_id: RecipientId) -> dict[str, Any]:
        """One recipient account. GET /v2/accounts/{accountId}."""
        result: dict[str, Any] = await c.get(f"/v2/accounts/{recipient_id}", tool="get_recipient")
        return result

    @reg.tool(S, title="Get recipient account requirements", read_only=True)
    async def get_account_requirements(
        source_currency: Currency, target_currency: Currency, source_amount: Annotated[float, Field(gt=0)]
    ) -> dict[str, Any]:
        """Fields required to create a recipient for a currency route. GET /v1/account-requirements."""
        data = await c.get(
            "/v1/account-requirements",
            {"source": source_currency, "target": target_currency, "sourceAmount": source_amount},
            tool="get_account_requirements",
        )
        return items(data)

    # --- rates, currencies, pricing ---------------------------------------------------------
    @reg.tool(S, title="Get exchange rate", read_only=True)
    async def get_exchange_rate(source: Currency, target: Currency, time: IsoDateTime | None = None) -> dict[str, Any]:
        """Wise mid-market rate now, or at a past `time`. GET /v1/rates."""
        data = await c.get(
            "/v1/rates", {"source": source, "target": target, "time": _iso(time)}, tool="get_exchange_rate"
        )
        return items(data)

    @reg.tool(S, title="Get exchange rate history", read_only=True)
    async def get_rate_history(
        source: Currency,
        target: Currency,
        start: IsoDateTime,
        end: IsoDateTime,
        group: Literal["day", "hour", "minute"] = "day",
    ) -> dict[str, Any]:
        """Historical rates over a period, grouped by day/hour/minute. GET /v1/rates?from&to&group."""
        data = await c.get(
            "/v1/rates",
            {"source": source, "target": target, "from": _iso(start), "to": _iso(end, end=True), "group": group},
            tool="get_rate_history",
        )
        return items(data)

    @reg.tool(S, title="List currencies", read_only=True)
    async def list_currencies() -> dict[str, Any]:
        """Currencies supported by Wise. GET /v1/currencies."""
        return items(await c.get("/v1/currencies", tool="list_currencies"))

    @reg.tool(S, title="Compare transfer prices", read_only=True, open_world=True)
    async def compare_prices(
        source_currency: Currency,
        target_currency: Currency,
        send_amount: Annotated[float, Field(gt=0, le=10_000_000)],
    ) -> dict[str, Any]:
        """Price estimate: Wise vs. banks/providers for sending an amount. Creates nothing.
        GET /v4/comparisons."""
        result: dict[str, Any] = await c.get(
            "/v4/comparisons",
            {"sourceCurrency": source_currency, "targetCurrency": target_currency, "sendAmount": send_amount},
            tool="compare_prices",
        )
        return result

    @reg.tool(S, title="Get quote", read_only=True)
    async def get_quote(profile_id: ProfileId, quote_id: Uuid) -> dict[str, Any]:
        """An existing quote. GET /v3/profiles/{profileId}/quotes/{quoteId}."""
        result: dict[str, Any] = await c.get(f"/v3/profiles/{profile_id}/quotes/{quote_id}", tool="get_quote")
        return result

    # --- invoices / payment requests (read) -----------------------------------------------
    @reg.tool(S, title="List payment requests / invoices", read_only=True)
    async def list_payment_requests(
        profile_id: ProfileId,
        status: Annotated[str | None, Field(pattern=r"^[A-Z_]{3,30}$", description="e.g. DRAFT, PUBLISHED")] = None,
        cursor: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        """Invoices and payment links. GET /v2/profiles/{profileId}/acquiring/payment-requests."""
        data = await c.get(
            f"/v2/profiles/{profile_id}/acquiring/payment-requests",
            {"status": status, "nextCursor": cursor},
            tool="list_payment_requests",
        )
        content = data.get("content", []) if isinstance(data, dict) else data
        return items(content, next_cursor=data.get("nextCursor") if isinstance(data, dict) else None)

    @reg.tool(S, title="Get payment request / invoice", read_only=True)
    async def get_payment_request(profile_id: ProfileId, payment_request_id: Uuid) -> dict[str, Any]:
        """One invoice / payment request. GET /v2/profiles/{profileId}/acquiring/payment-requests/{id}."""
        result: dict[str, Any] = await c.get(
            f"/v2/profiles/{profile_id}/acquiring/payment-requests/{payment_request_id}",
            tool="get_payment_request",
        )
        return result

    @reg.tool(S, title="Get invoice currency options", read_only=True)
    async def get_invoice_currency_options(profile_id: ProfileId) -> dict[str, Any]:
        """Balances usable to receive invoice payments, and currency eligibility.
        GET /v1/profiles/{profileId}/acquiring/requesting-configuration/currency-options."""
        result: dict[str, Any] = await c.get(
            f"/v1/profiles/{profile_id}/acquiring/requesting-configuration/currency-options",
            tool="get_invoice_currency_options",
        )
        return result

    # --- webhooks & batch groups (read) ---------------------------------------------------
    @reg.tool(S, title="List webhook subscriptions", read_only=True)
    async def list_webhook_subscriptions(profile_id: ProfileId) -> dict[str, Any]:
        """Profile webhook subscriptions. GET /v3/profiles/{profileId}/subscriptions."""
        return items(await c.get(f"/v3/profiles/{profile_id}/subscriptions", tool="list_webhook_subscriptions"))

    @reg.tool(S, title="Get batch group", read_only=True)
    async def get_batch_group(profile_id: ProfileId, batch_group_id: Uuid) -> dict[str, Any]:
        """A batch transfer group. GET /v3/profiles/{profileId}/batch-groups/{batchGroupId}."""
        result: dict[str, Any] = await c.get(
            f"/v3/profiles/{profile_id}/batch-groups/{batch_group_id}", tool="get_batch_group"
        )
        return result
