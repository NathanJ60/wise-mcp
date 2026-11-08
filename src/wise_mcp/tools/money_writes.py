"""Money-moving tools: transfers, batch transfers and balance moves.

Every operation that actually moves funds is split into ``prepare_*`` (read-only: fetches the
real amounts, checks caps and allowlists, returns a summary and a single-use confirmation token)
and ``execute_*`` (consumes the token, re-checks everything against fresh data, then calls Wise).
"""

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field

from wise_mcp.errors import WiseMcpError
from wise_mcp.policy import Scope
from wise_mcp.tools.common import (
    BalanceId,
    ConfirmToken,
    Currency,
    PositiveAmount,
    ProfileId,
    RecipientId,
    Registrar,
    TransferId,
    Uuid,
)
from wise_mcp.tools.money import consume, prepare, require_confirm, to_decimal

Reference = Annotated[str | None, Field(max_length=100, description="Payment reference shown to the recipient.")]
CustomerTxId = Annotated[
    str | None,
    Field(pattern=r"^[0-9a-fA-F-]{36}$", description="Idempotency UUID; generated if omitted."),
]


def register_transfers(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.TRANSFERS

    @reg.tool(S, title="Validate transfer requirements", read_only=True)
    async def validate_transfer_requirements(
        target_account: RecipientId, quote_uuid: Uuid, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Check which extra fields a transfer needs. Creates nothing. POST /v1/transfer-requirements."""
        data = await c.request(
            "POST",
            "/v1/transfer-requirements",
            json={"targetAccount": target_account, "quoteUuid": quote_uuid, "details": details or {}},
            tool="validate_transfer_requirements",
        )
        return {"items": data}

    @reg.tool(S, title="Create transfer (unfunded)", read_only=False, destructive=False, idempotent=True)
    async def create_transfer(
        target_account: RecipientId,
        quote_uuid: Uuid,
        reference: Reference = None,
        customer_transaction_id: CustomerTxId = None,
        transfer_purpose: Annotated[str | None, Field(max_length=100)] = None,
        source_of_funds: Annotated[str | None, Field(max_length=100)] = None,
    ) -> dict[str, Any]:
        """Create a transfer from a quote. The transfer is NOT funded: no money moves until
        prepare_fund_transfer + execute_fund_transfer. POST /v1/transfers."""
        app.check_recipient(target_account, tool="create_transfer")
        details: dict[str, Any] = {}
        if reference:
            details["reference"] = reference
        if transfer_purpose:
            details["transferPurpose"] = transfer_purpose
        if source_of_funds:
            details["sourceOfFunds"] = source_of_funds
        result: dict[str, Any] = await c.request(
            "POST",
            "/v1/transfers",
            json={
                "targetAccount": target_account,
                "quoteUuid": quote_uuid,
                "customerTransactionId": customer_transaction_id or str(uuid.uuid4()),
                "details": details,
            },
            tool="create_transfer",
        )
        return result

    @reg.tool(S, title="Cancel transfer", read_only=False, destructive=True, idempotent=True)
    async def cancel_transfer(transfer_id: TransferId, confirm: bool = False) -> dict[str, Any]:
        """Cancel a transfer that has not been sent yet. Requires confirm=true.
        PUT /v1/transfers/{transferId}/cancel."""
        require_confirm(app, "cancel_transfer", confirm, "This cancels the transfer.")
        result: dict[str, Any] = await c.request("PUT", f"/v1/transfers/{transfer_id}/cancel", tool="cancel_transfer")
        return result

    async def _transfer_facts(transfer_id: int, tool: str) -> tuple[dict[str, Any], str, Decimal]:
        t = await c.get(f"/v1/transfers/{transfer_id}", tool=tool)
        app.check_recipient(t.get("targetAccount"), tool=tool)
        return t, str(t["sourceCurrency"]), to_decimal(t["sourceValue"])

    @reg.tool(S, title="Prepare transfer funding (step 1/2)", read_only=True)
    async def prepare_fund_transfer(profile_id: ProfileId, transfer_id: TransferId) -> dict[str, Any]:
        """Step 1 of 2 before paying a transfer from your balance. Moves nothing: returns the
        amount, recipient and a single-use confirmation token for execute_fund_transfer."""
        app.policy.check_profile(profile_id)
        t, cur, amount = await _transfer_facts(transfer_id, "prepare_fund_transfer")
        bound = {"profile_id": profile_id, "transfer_id": transfer_id, "currency": cur, "amount": str(amount)}
        return prepare(
            app,
            "prepare_fund_transfer",
            "execute_fund_transfer",
            bound,
            {
                "transfer_id": transfer_id,
                "status": t.get("status"),
                "target_account": t.get("targetAccount"),
                "source": f"{amount} {cur}",
                "target": f"{t.get('targetValue')} {t.get('targetCurrency')}",
                "rate": t.get("rate"),
                "reference": (t.get("details") or {}).get("reference"),
            },
            currency=cur,
            amount=amount,
        )

    @reg.tool(
        S,
        title="Fund transfer (step 2/2, moves money)",
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    )
    async def execute_fund_transfer(
        profile_id: ProfileId, transfer_id: TransferId, confirmation_token: ConfirmToken
    ) -> dict[str, Any]:
        """Step 2 of 2: pay the transfer from your Wise balance. MOVES MONEY. Requires the token from
        prepare_fund_transfer. POST /v3/profiles/{profileId}/transfers/{transferId}/payments."""
        _, cur, amount = await _transfer_facts(transfer_id, "execute_fund_transfer")
        bound = {"profile_id": profile_id, "transfer_id": transfer_id, "currency": cur, "amount": str(amount)}
        consume(
            app,
            "execute_fund_transfer",
            "execute_fund_transfer",
            confirmation_token,
            bound,
            currency=cur,
            amount=amount,
        )
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v3/profiles/{profile_id}/transfers/{transfer_id}/payments",
            json={"type": "BALANCE"},
            tool="execute_fund_transfer",
        )
        app.limiter.record(cur, amount)
        return result


def register_batch(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.BATCH_TRANSFERS

    @reg.tool(S, title="Create batch group", read_only=False, destructive=False)
    async def create_batch_group(
        profile_id: ProfileId, source_currency: Currency, name: Annotated[str, Field(min_length=1, max_length=100)]
    ) -> dict[str, Any]:
        """Create an empty batch transfer group. POST /v3/profiles/{profileId}/batch-groups."""
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v3/profiles/{profile_id}/batch-groups",
            json={"sourceCurrency": source_currency, "name": name},
            tool="create_batch_group",
        )
        return result

    @reg.tool(S, title="Add transfer to batch group", read_only=False, destructive=False, idempotent=True)
    async def add_batch_transfer(
        profile_id: ProfileId,
        batch_group_id: Uuid,
        target_account: RecipientId,
        quote_uuid: Uuid,
        reference: Reference = None,
        customer_transaction_id: CustomerTxId = None,
    ) -> dict[str, Any]:
        """Add an (unfunded) transfer to a batch group.
        POST /v3/profiles/{profileId}/batch-groups/{batchGroupId}/transfers."""
        app.check_recipient(target_account, tool="add_batch_transfer")
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v3/profiles/{profile_id}/batch-groups/{batch_group_id}/transfers",
            json={
                "targetAccount": target_account,
                "quoteUuid": quote_uuid,
                "customerTransactionId": customer_transaction_id or str(uuid.uuid4()),
                "details": {"reference": reference} if reference else {},
            },
            tool="add_batch_transfer",
        )
        return result

    @reg.tool(S, title="Complete or cancel batch group", read_only=False, destructive=True, idempotent=True)
    async def update_batch_group_status(
        profile_id: ProfileId,
        batch_group_id: Uuid,
        status: Literal["COMPLETED", "CANCELLED"],
        version: Annotated[int, Field(ge=0, description="Current `version` from get_batch_group.")],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Close a batch group for funding (COMPLETED) or cancel it (CANCELLED). Requires confirm=true.
        PATCH /v3/profiles/{profileId}/batch-groups/{batchGroupId}."""
        require_confirm(app, "update_batch_group_status", confirm, f"This sets the batch group to {status}.")
        result: dict[str, Any] = await c.request(
            "PATCH",
            f"/v3/profiles/{profile_id}/batch-groups/{batch_group_id}",
            json={"status": status, "version": version},
            tool="update_batch_group_status",
        )
        return result

    async def _batch_facts(profile_id: int, batch_group_id: str, tool: str) -> tuple[dict[str, Any], str, Decimal]:
        g = await c.get(f"/v3/profiles/{profile_id}/batch-groups/{batch_group_id}", tool=tool)
        ids = [int(i) for i in (g.get("transferIds") or [])]
        if not ids:
            raise WiseMcpError("Batch group contains no transfers")
        total = Decimal("0")
        currency = str(g.get("sourceCurrency"))
        for tid in ids:
            t = await c.get(f"/v1/transfers/{tid}", tool=tool)
            app.check_recipient(t.get("targetAccount"), tool=tool)
            if str(t.get("sourceCurrency")) != currency:
                raise WiseMcpError("Batch transfers use mixed source currencies; refusing")
            total += to_decimal(t["sourceValue"])
        return g, currency, total

    @reg.tool(S, title="Prepare batch funding (step 1/2)", read_only=True)
    async def prepare_fund_batch_group(profile_id: ProfileId, batch_group_id: Uuid) -> dict[str, Any]:
        """Step 1 of 2 before paying a whole batch from your balance. Moves nothing."""
        app.policy.check_profile(profile_id)
        g, cur, total = await _batch_facts(profile_id, batch_group_id, "prepare_fund_batch_group")
        bound = {
            "profile_id": profile_id,
            "batch_group_id": batch_group_id,
            "currency": cur,
            "amount": str(total),
            "transfers": sorted(g.get("transferIds") or []),
        }
        return prepare(
            app,
            "prepare_fund_batch_group",
            "execute_fund_batch_group",
            bound,
            {
                "batch_group_id": batch_group_id,
                "name": g.get("name"),
                "status": g.get("status"),
                "transfer_count": len(g.get("transferIds") or []),
                "total": f"{total} {cur}",
            },
            currency=cur,
            amount=total,
        )

    @reg.tool(
        S,
        title="Fund batch group (step 2/2, moves money)",
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    )
    async def execute_fund_batch_group(
        profile_id: ProfileId, batch_group_id: Uuid, confirmation_token: ConfirmToken
    ) -> dict[str, Any]:
        """Step 2 of 2: pay every transfer of a COMPLETED batch group from balance. MOVES MONEY.
        POST /v3/profiles/{profileId}/batch-payments/{batchGroupId}/payments."""
        g, cur, total = await _batch_facts(profile_id, batch_group_id, "execute_fund_batch_group")
        bound = {
            "profile_id": profile_id,
            "batch_group_id": batch_group_id,
            "currency": cur,
            "amount": str(total),
            "transfers": sorted(g.get("transferIds") or []),
        }
        consume(
            app,
            "execute_fund_batch_group",
            "execute_fund_batch_group",
            confirmation_token,
            bound,
            currency=cur,
            amount=total,
        )
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v3/profiles/{profile_id}/batch-payments/{batch_group_id}/payments",
            json={"type": "BALANCE"},
            tool="execute_fund_batch_group",
        )
        app.limiter.record(cur, total)
        return result


def register_balance_moves(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.BALANCE_MOVES

    @reg.tool(S, title="Open balance or jar", read_only=False, destructive=False)
    async def open_balance(
        profile_id: ProfileId,
        currency: Currency,
        type: Literal["STANDARD", "SAVINGS"] = "STANDARD",
        name: Annotated[str | None, Field(max_length=60, description="Required for SAVINGS (jars).")] = None,
    ) -> dict[str, Any]:
        """Open a currency balance (STANDARD) or a jar (SAVINGS). Moves no money.
        POST /v4/profiles/{profileId}/balances."""
        if type == "SAVINGS" and not name:
            raise WiseMcpError("A name is required for a SAVINGS jar")
        body: dict[str, Any] = {"currency": currency, "type": type}
        if name:
            body["name"] = name
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v4/profiles/{profile_id}/balances",
            json=body,
            headers={"X-idempotence-uuid": str(uuid.uuid4())},
            tool="open_balance",
        )
        return result

    @reg.tool(S, title="Close balance or jar", read_only=False, destructive=True, idempotent=True)
    async def close_balance(profile_id: ProfileId, balance_id: BalanceId, confirm: bool = False) -> dict[str, Any]:
        """Close an empty balance or jar. Requires confirm=true.
        DELETE /v4/profiles/{profileId}/balances/{balanceId}."""
        require_confirm(app, "close_balance", confirm, "This closes the balance.")
        result: dict[str, Any] = await c.request(
            "DELETE", f"/v4/profiles/{profile_id}/balances/{balance_id}", tool="close_balance"
        )
        return result or {"closed": True, "balance_id": balance_id}

    async def _quote_facts(profile_id: int, quote_id: str, tool: str) -> tuple[dict[str, Any], str, Decimal]:
        q = await c.get(f"/v3/profiles/{profile_id}/quotes/{quote_id}", tool=tool)
        amount = q.get("sourceAmount")
        if amount is None:
            raise WiseMcpError("Quote has no sourceAmount")
        return q, str(q["sourceCurrency"]), to_decimal(amount)

    @reg.tool(S, title="Prepare currency conversion (step 1/2)", read_only=True)
    async def prepare_balance_conversion(profile_id: ProfileId, quote_id: Uuid) -> dict[str, Any]:
        """Step 1 of 2 to convert between two of your balances using a quote created with
        create_quote (payOut=BALANCE). Moves nothing; returns a single-use confirmation token."""
        app.policy.check_profile(profile_id)
        q, cur, amount = await _quote_facts(profile_id, quote_id, "prepare_balance_conversion")
        bound = {"profile_id": profile_id, "quote_id": quote_id, "currency": cur, "amount": str(amount)}
        return prepare(
            app,
            "prepare_balance_conversion",
            "execute_balance_conversion",
            bound,
            {
                "quote_id": quote_id,
                "sell": f"{amount} {cur}",
                "buy": f"{q.get('targetAmount')} {q.get('targetCurrency')}",
                "rate": q.get("rate"),
                "quote_expires": q.get("expirationTime"),
            },
            currency=cur,
            amount=amount,
        )

    @reg.tool(S, title="Convert currency (step 2/2, moves money)", read_only=False, destructive=True, idempotent=True)
    async def execute_balance_conversion(
        profile_id: ProfileId, quote_id: Uuid, confirmation_token: ConfirmToken
    ) -> dict[str, Any]:
        """Step 2 of 2: execute the conversion. MOVES MONEY between your balances.
        POST /v2/profiles/{profileId}/balance-movements."""
        _, cur, amount = await _quote_facts(profile_id, quote_id, "execute_balance_conversion")
        bound = {"profile_id": profile_id, "quote_id": quote_id, "currency": cur, "amount": str(amount)}
        key = consume(
            app,
            "execute_balance_conversion",
            "execute_balance_conversion",
            confirmation_token,
            bound,
            currency=cur,
            amount=amount,
        )
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v2/profiles/{profile_id}/balance-movements",
            json={"quoteId": quote_id},
            headers={"X-idempotence-uuid": key},
            tool="execute_balance_conversion",
        )
        app.limiter.record(cur, amount)
        return result

    async def _move_facts(profile_id: int, source: int, target: int, currency: str, tool: str) -> dict[str, Any]:
        if source == target:
            raise WiseMcpError("Source and target balances must differ")
        s = await c.get(f"/v4/profiles/{profile_id}/balances/{source}", tool=tool)
        t = await c.get(f"/v4/profiles/{profile_id}/balances/{target}", tool=tool)
        if s.get("currency") != currency or t.get("currency") != currency:
            raise WiseMcpError("Both balances must be in the move currency; use a conversion otherwise")
        return {
            "from": f"{s.get('type')} {s.get('name') or s.get('currency')} (#{source})",
            "to": f"{t.get('type')} {t.get('name') or t.get('currency')} (#{target})",
        }

    @reg.tool(S, title="Prepare move between balances/jars (step 1/2)", read_only=True)
    async def prepare_balance_move(
        profile_id: ProfileId,
        source_balance_id: BalanceId,
        target_balance_id: BalanceId,
        amount: PositiveAmount,
        currency: Currency,
    ) -> dict[str, Any]:
        """Step 1 of 2 to move money between two of your same-currency balances (e.g. into a jar).
        Moves nothing; returns a single-use confirmation token."""
        app.policy.check_profile(profile_id)
        facts = await _move_facts(profile_id, source_balance_id, target_balance_id, currency, "prepare_balance_move")
        value = to_decimal(amount)
        bound = {
            "profile_id": profile_id,
            "source": source_balance_id,
            "target": target_balance_id,
            "currency": currency,
            "amount": str(value),
        }
        return prepare(
            app,
            "prepare_balance_move",
            "execute_balance_move",
            bound,
            {**facts, "amount": f"{value} {currency}"},
            currency=currency,
            amount=value,
        )

    @reg.tool(
        S,
        title="Move between balances/jars (step 2/2, moves money)",
        read_only=False,
        destructive=True,
        idempotent=True,
    )
    async def execute_balance_move(
        profile_id: ProfileId,
        source_balance_id: BalanceId,
        target_balance_id: BalanceId,
        amount: PositiveAmount,
        currency: Currency,
        confirmation_token: ConfirmToken,
    ) -> dict[str, Any]:
        """Step 2 of 2: move the money. POST /v2/profiles/{profileId}/balance-movements."""
        await _move_facts(profile_id, source_balance_id, target_balance_id, currency, "execute_balance_move")
        value = to_decimal(amount)
        bound = {
            "profile_id": profile_id,
            "source": source_balance_id,
            "target": target_balance_id,
            "currency": currency,
            "amount": str(value),
        }
        key = consume(
            app,
            "execute_balance_move",
            "execute_balance_move",
            confirmation_token,
            bound,
            currency=currency,
            amount=value,
        )
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v2/profiles/{profile_id}/balance-movements",
            json={
                "sourceBalanceId": source_balance_id,
                "targetBalanceId": target_balance_id,
                "amount": {"value": float(value), "currency": currency},
            },
            headers={"X-idempotence-uuid": key},
            tool="execute_balance_move",
        )
        app.limiter.record(currency, value)
        return result
