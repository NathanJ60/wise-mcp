"""Webhook (scope: webhooks), recipient (scope: recipients) and quote (scope: quotes) write tools."""

from typing import Annotated, Any, Literal

from pydantic import Field

from wise_mcp.errors import WiseMcpError
from wise_mcp.policy import Scope
from wise_mcp.tools.common import Currency, ProfileId, RecipientId, Registrar, Uuid
from wise_mcp.tools.money import require_confirm


def register_webhooks(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.WEBHOOKS

    @reg.tool(S, title="Create webhook subscription", read_only=False, destructive=False, open_world=True)
    async def create_webhook_subscription(
        profile_id: ProfileId,
        name: Annotated[str, Field(min_length=1, max_length=100)],
        trigger_on: Annotated[
            str, Field(pattern=r"^[a-z0-9#._-]{3,80}$", description="Event type, e.g. transfers#state-change")
        ],
        url: Annotated[str, Field(pattern=r"^https://[^\s]{4,500}$", description="HTTPS endpoint only.")],
        version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] = "2.0.0",
    ) -> dict[str, Any]:
        """Subscribe an HTTPS URL to profile events. POST /v3/profiles/{profileId}/subscriptions."""
        result: dict[str, Any] = await c.request(
            "POST",
            f"/v3/profiles/{profile_id}/subscriptions",
            json={"name": name, "trigger_on": trigger_on, "delivery": {"version": version, "url": url}},
            tool="create_webhook_subscription",
        )
        return result

    @reg.tool(S, title="Delete webhook subscription", read_only=False, destructive=True, idempotent=True)
    async def delete_webhook_subscription(
        profile_id: ProfileId, subscription_id: Uuid, confirm: bool = False
    ) -> dict[str, Any]:
        """Delete a webhook subscription. Requires confirm=true.
        DELETE /v3/profiles/{profileId}/subscriptions/{subscriptionId}."""
        require_confirm(app, "delete_webhook_subscription", confirm, "This deletes the subscription.")
        await c.request(
            "DELETE", f"/v3/profiles/{profile_id}/subscriptions/{subscription_id}", tool="delete_webhook_subscription"
        )
        return {"deleted": True, "subscription_id": subscription_id}


def register_recipients(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.RECIPIENTS

    @reg.tool(S, title="Create recipient", read_only=False, destructive=False)
    async def create_recipient(
        profile_id: ProfileId,
        currency: Currency,
        type: Annotated[str, Field(pattern=r"^[a-z_]{2,40}$", description="e.g. iban, sort_code, aba")],
        account_holder_name: Annotated[str, Field(min_length=2, max_length=140)],
        details: Annotated[
            dict[str, Any], Field(description="Fields from get_account_requirements, e.g. {'iban': ...}")
        ],
        owned_by_customer: bool = False,
    ) -> dict[str, Any]:
        """Create a recipient (payee) account. Creating a recipient does not move money.
        POST /v1/accounts."""
        result: dict[str, Any] = await c.request(
            "POST",
            "/v1/accounts",
            json={
                "profile": profile_id,
                "currency": currency,
                "type": type,
                "accountHolderName": account_holder_name,
                "ownedByCustomer": owned_by_customer,
                "details": details,
            },
            tool="create_recipient",
        )
        return result

    @reg.tool(S, title="Deactivate recipient", read_only=False, destructive=True, idempotent=True)
    async def delete_recipient(recipient_id: RecipientId, confirm: bool = False) -> dict[str, Any]:
        """Deactivate (delete) a recipient account. Requires confirm=true. DELETE /v2/accounts/{id}."""
        require_confirm(app, "delete_recipient", confirm, "This deactivates the recipient.")
        await c.request("DELETE", f"/v2/accounts/{recipient_id}", tool="delete_recipient")
        return {"deactivated": True, "recipient_id": recipient_id}


def register_quotes(reg: Registrar) -> None:
    app, c, S = reg.app, reg.app.client, Scope.QUOTES

    @reg.tool(S, title="Create quote", read_only=False, destructive=False)
    async def create_quote(
        profile_id: ProfileId,
        source_currency: Currency,
        target_currency: Currency,
        source_amount: Annotated[float | None, Field(gt=0, le=10_000_000)] = None,
        target_amount: Annotated[float | None, Field(gt=0, le=10_000_000)] = None,
        target_account: RecipientId | None = None,
        pay_out: Literal["BANK_TRANSFER", "BALANCE", "SWIFT", "SWIFT_OUR", "INTERAC"] | None = None,
    ) -> dict[str, Any]:
        """Create a quote (price + rate lock). A quote alone moves no money; it is needed for
        transfers and balance conversions. Give exactly one of source_amount / target_amount.
        POST /v3/profiles/{profileId}/quotes."""
        if (source_amount is None) == (target_amount is None):
            raise WiseMcpError("Provide exactly one of source_amount or target_amount")
        app.check_recipient(target_account, tool="create_quote")
        body: dict[str, Any] = {"sourceCurrency": source_currency, "targetCurrency": target_currency}
        if source_amount is not None:
            body["sourceAmount"] = source_amount
        if target_amount is not None:
            body["targetAmount"] = target_amount
        if target_account is not None:
            body["targetAccount"] = target_account
        if pay_out is not None:
            body["payOut"] = pay_out
        result: dict[str, Any] = await c.request(
            "POST", f"/v3/profiles/{profile_id}/quotes", json=body, tool="create_quote"
        )
        return result

    @reg.tool(S, title="Update quote", read_only=False, destructive=False, idempotent=True)
    async def update_quote(
        profile_id: ProfileId,
        quote_id: Uuid,
        target_account: RecipientId,
        pay_out: Literal["BANK_TRANSFER", "BALANCE", "SWIFT", "SWIFT_OUR", "INTERAC"] | None = None,
    ) -> dict[str, Any]:
        """Attach a recipient to a quote (refreshes fees). PATCH /v3/profiles/{profileId}/quotes/{quoteId}."""
        app.check_recipient(target_account, tool="update_quote")
        body: dict[str, Any] = {"targetAccount": target_account}
        if pay_out:
            body["payOut"] = pay_out
        result: dict[str, Any] = await c.request(
            "PATCH",
            f"/v3/profiles/{profile_id}/quotes/{quote_id}",
            json=body,
            headers={"Content-Type": "application/merge-patch+json"},
            tool="update_quote",
        )
        return result
