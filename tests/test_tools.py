"""Every tool, called through a real in-process MCP client against mocked Wise endpoints."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp import Client

from tests.conftest import EVERYTHING, audit_lines, writes_sent

pytestmark = pytest.mark.anyio

READ_CASES: list[tuple[str, dict[str, Any], str, Any]] = [
    ("list_profiles", {}, "/v2/profiles", [{"id": 1, "type": "BUSINESS", "businessName": "X"}]),
    ("get_profile", {"profile_id": 1}, "/v2/profiles/1", {"id": 1}),
    ("get_current_user", {}, "/v1/me", {"id": 3}),
    ("list_balances", {"profile_id": 1}, "/v4/profiles/1/balances", [{"id": 2}]),
    ("get_balance", {"profile_id": 1, "balance_id": 2}, "/v4/profiles/1/balances/2", {"id": 2}),
    ("get_total_funds", {"profile_id": 1, "currency": "USD"}, "/v1/profiles/1/total-funds/USD", {"totalWorth": {}}),
    (
        "get_balance_capacity",
        {"profile_id": 1, "currency": "USD"},
        "/v1/profiles/1/balance-capacity",
        {"hasLimit": False},
    ),
    ("list_account_details_orders", {"profile_id": 1, "currency": "EUR"}, "/v3/profiles/1/account-details-orders", []),
    (
        "list_activities",
        {"profile_id": 1, "since": "2026-01-01", "size": 5},
        "/v1/profiles/1/activities",
        {"cursor": "c2", "activities": [{"id": "a", "type": "CARD_PAYMENT"}]},
    ),
    (
        "get_balance_statement",
        {
            "profile_id": 1,
            "balance_id": 2,
            "currency": "EUR",
            "interval_start": "2026-01-01",
            "interval_end": "2026-01-31",
        },
        "/v1/profiles/1/balance-statements/2/statement.json",
        {"transactions": []},
    ),
    ("list_transfers", {"profile_id": 1, "limit": 5}, "/v1/transfers", [{"id": 7}]),
    ("get_transfer", {"transfer_id": 7}, "/v1/transfers/7", {"id": 7}),
    ("list_transfer_payments", {"transfer_id": 7}, "/v1/transfers/7/payments", []),
    ("get_delivery_estimate", {"transfer_id": 7}, "/v1/delivery-estimates/7", {"estimatedDeliveryDate": "x"}),
    ("list_recipients", {"profile_id": 1, "currency": "EUR"}, "/v1/accounts", [{"id": 5}]),
    ("get_recipient", {"recipient_id": 5}, "/v2/accounts/5", {"id": 5}),
    (
        "get_account_requirements",
        {"source_currency": "EUR", "target_currency": "GBP", "source_amount": 10},
        "/v1/account-requirements",
        [{"type": "sort_code"}],
    ),
    ("get_exchange_rate", {"source": "EUR", "target": "USD"}, "/v1/rates", [{"rate": 1.1}]),
    (
        "get_rate_history",
        {"source": "EUR", "target": "USD", "start": "2026-01-01", "end": "2026-01-05"},
        "/v1/rates",
        [{"rate": 1.1}],
    ),
    ("list_currencies", {}, "/v1/currencies", [{"code": "EUR"}]),
    (
        "compare_prices",
        {"source_currency": "EUR", "target_currency": "USD", "send_amount": 100},
        "/v4/comparisons",
        {"providers": []},
    ),
    ("get_quote", {"profile_id": 1, "quote_id": "q-1"}, "/v3/profiles/1/quotes/q-1", {"id": "q-1"}),
    (
        "list_payment_requests",
        {"profile_id": 1},
        "/v2/profiles/1/acquiring/payment-requests",
        {"content": [{"id": "pr"}], "nextCursor": None},
    ),
    (
        "get_payment_request",
        {"profile_id": 1, "payment_request_id": "pr-1"},
        "/v2/profiles/1/acquiring/payment-requests/pr-1",
        {"id": "pr-1"},
    ),
    (
        "get_invoice_currency_options",
        {"profile_id": 1},
        "/v1/profiles/1/acquiring/requesting-configuration/currency-options",
        {"balances": []},
    ),
    ("list_webhook_subscriptions", {"profile_id": 1}, "/v3/profiles/1/subscriptions", []),
    (
        "get_batch_group",
        {"profile_id": 1, "batch_group_id": "bg-1"},
        "/v3/profiles/1/batch-groups/bg-1",
        {"id": "bg-1"},
    ),
]


@pytest.mark.parametrize("tool,args,path,payload", READ_CASES, ids=[c[0] for c in READ_CASES])
async def test_read_tools(make_server, mock_api: respx.MockRouter, tool, args, path, payload) -> None:
    server, _ = make_server()
    route = mock_api.get(path).mock(return_value=httpx.Response(200, json=payload))
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    assert route.called
    assert isinstance(result.structured_content, dict)


async def test_activity_and_statement_params(make_server, mock_api) -> None:
    server, _ = make_server()
    acts = mock_api.get("/v1/profiles/1/activities").mock(
        return_value=httpx.Response(200, json={"cursor": "next", "activities": []})
    )
    stmt = mock_api.get("/v1/profiles/1/balance-statements/2/statement.json").mock(
        return_value=httpx.Response(200, json={})
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "list_activities",
            {"profile_id": 1, "monetary_resource_type": "TRANSFER", "until": "2026-02-01", "cursor": "abc"},
        )
        assert r.structured_content["next_cursor"] == "next"
        q = acts.calls[0].request.url.params
        assert q["monetaryResourceType"] == "TRANSFER" and q["nextCursor"] == "abc"
        assert q["until"] == "2026-02-01T23:59:59.999Z"
        too_long = await client.call_tool(
            "get_balance_statement",
            {
                "profile_id": 1,
                "balance_id": 2,
                "currency": "EUR",
                "interval_start": "2024-01-01",
                "interval_end": "2026-01-01",
            },
        )
        assert too_long.is_error and "469" in too_long.content[0].text
        assert not stmt.called


async def test_account_details_split_active_and_available(make_server, mock_api) -> None:
    server, _ = make_server()
    mock_api.get("/v1/profiles/1/account-details").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "currency": {"code": "EUR"},
                    "status": "ACTIVE",
                    "balanceId": 9,
                    "title": "EUR",
                    "receiveOptions": [
                        {"type": "LOCAL", "title": "Local", "details": [{"title": "IBAN", "body": "BE00"}]}
                    ],
                },
                {
                    "id": 2,
                    "currency": {"code": "JPY"},
                    "status": "AVAILABLE",
                    "title": "JPY",
                    "receiveOptions": [{"details": [{"title": "Account", "body": "xxxx"}]}],
                },
            ],
        )
    )
    async with Client(server) as client:
        r = (await client.call_tool("get_account_details", {"profile_id": 1})).structured_content
        assert r["active_count"] == 1 and r["active"][0]["currency"] == "EUR"
        assert r["not_opened_count"] == 1 and "xxxx" not in json.dumps(r)
        r2 = await client.call_tool("get_account_details", {"profile_id": 1, "include_not_opened": True})
        assert r2.structured_content["not_opened"][0]["currency"] == "JPY"


async def test_profile_allowlist_filters_and_blocks(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ALLOWED_PROFILE_IDS="1")
    mock_api.get("/v2/profiles").mock(return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}]))
    async with Client(server) as client:
        r = await client.call_tool("list_profiles", {})
        assert [p["id"] for p in r.structured_content["items"]] == [1]
        blocked = await client.call_tool("list_balances", {"profile_id": 2})
        assert blocked.is_error and "WISE_ALLOWED_PROFILE_IDS" in blocked.content[0].text


async def test_multi_currency_account(make_server, mock_api) -> None:
    server, _ = make_server()
    mock_api.get("/v4/profiles/1/multi-currency-account").mock(return_value=httpx.Response(200, json={"id": 1}))
    base = "/v2/borderless-accounts-configuration/profiles/1"
    mock_api.get(f"{base}/available-currencies").mock(return_value=httpx.Response(200, json=["EUR"]))
    mock_api.get(f"{base}/payin-currencies").mock(return_value=httpx.Response(200, json=["USD"]))
    async with Client(server) as client:
        r = (await client.call_tool("get_multi_currency_account", {"profile_id": 1})).structured_content
    assert r == {"account": {"id": 1}, "balance_currencies": ["EUR"], "payin_currencies": ["USD"]}


async def test_get_permissions(make_server) -> None:
    server, _ = make_server(WISE_ENABLE="invoices", WISE_DENY="transfers")
    async with Client(server) as client:
        r = (await client.call_tool("get_permissions", {})).structured_content
    assert "create_invoice_draft" in r["registered_tools"]
    assert "transfers" in r["denied_scopes"]


# --- invoices ----------------------------------------------------------------------------------
async def test_invoice_draft_flow_and_publish_confirmation(make_server, mock_api) -> None:
    server, app = make_server(WISE_ENABLE="invoices")
    mock_api.get("/v4/profiles/1/balances/9").mock(return_value=httpx.Response(200, json={"currency": "EUR"}))
    create = mock_api.post("/v2/profiles/1/acquiring/payment-requests").mock(
        return_value=httpx.Response(200, json={"id": "pr-1", "invoice": {"invoiceNumber": "INV-7"}})
    )
    fill = mock_api.put("/v2/profiles/1/acquiring/payment-requests/pr-1").mock(
        return_value=httpx.Response(200, json={"id": "pr-1", "status": "DRAFT"})
    )
    publish = mock_api.put("/v2/profiles/1/acquiring/payment-requests/pr-1/status").mock(
        return_value=httpx.Response(200, json={"id": "pr-1", "status": "PUBLISHED"})
    )
    async with Client(server) as client:
        r = await client.call_tool(
            "create_invoice_draft",
            {
                "profile_id": 1,
                "balance_id": 9,
                "line_items": [{"name": "Dev", "unit_price": 100, "quantity": 2, "tax": {"percentage": 20}}],
                "payer": {"name": "ACME", "email": "a@b.co", "address": {"country_iso3_code": "fra"}},
                "message": "VAT FR123",
            },
        )
        assert not r.is_error, r.content
        assert r.structured_content["status"] == "DRAFT"
        body = json.loads(fill.calls[0].request.content)
        assert body["invoiceNumber"] == "INV-7"
        assert body["selectedPaymentMethods"] == ["BANK_TRANSFER"]
        assert body["lineItems"][0]["unitPrice"] == {"value": 100.0, "currency": "EUR"}
        assert body["payer"]["address"]["countryIso3Code"] == "FRA"
        assert json.loads(create.calls[0].request.content)["lineItems"] == []
        assert not publish.called

        refused = await client.call_tool("publish_invoice", {"profile_id": 1, "payment_request_id": "pr-1"})
        assert refused.is_error and not publish.called
        ok = await client.call_tool("publish_invoice", {"profile_id": 1, "payment_request_id": "pr-1", "confirm": True})
        assert ok.structured_content["status"] == "PUBLISHED"
        assert json.loads(publish.calls[0].request.content) == {"status": "PUBLISHED"}
        assert (await client.call_tool("cancel_invoice", {"profile_id": 1, "payment_request_id": "pr-1"})).is_error
        cancelled = await client.call_tool(
            "cancel_invoice", {"profile_id": 1, "payment_request_id": "pr-1", "confirm": True}
        )
        assert not cancelled.is_error
        assert json.loads(publish.calls[1].request.content) == {"status": "INVALIDATED"}
        upd = await client.call_tool(
            "update_invoice_draft",
            {
                "profile_id": 1,
                "payment_request_id": "pr-1",
                "balance_id": 9,
                "due_date": "2026-12-31",
                "line_items": [{"name": "Dev", "unit_price": 1}],
                "payer": {"name": "ACME"},
            },
        )
        assert not upd.is_error
    events = [e["event"] for e in audit_lines(app)]
    assert events.count("write_ok") == 5 and "write_blocked" in events


# --- webhooks / recipients / quotes -------------------------------------------------------------
async def test_webhooks_recipients_quotes(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE="webhooks,recipients,quotes", WISE_ALLOWED_RECIPIENT_IDS="5")
    mock_api.post("/v3/profiles/1/subscriptions").mock(return_value=httpx.Response(200, json={"id": "s-1"}))
    mock_api.delete("/v3/profiles/1/subscriptions/s-1").mock(return_value=httpx.Response(204))
    mock_api.post("/v1/accounts").mock(return_value=httpx.Response(200, json={"id": 5}))
    mock_api.delete("/v2/accounts/5").mock(return_value=httpx.Response(200, json={}))
    quote = mock_api.post("/v3/profiles/1/quotes").mock(return_value=httpx.Response(200, json={"id": "q"}))
    patch = mock_api.patch("/v3/profiles/1/quotes/q").mock(return_value=httpx.Response(200, json={"id": "q"}))
    async with Client(server) as client:
        bad_url = await client.call_tool(
            "create_webhook_subscription",
            {"profile_id": 1, "name": "n", "trigger_on": "transfers#state-change", "url": "http://x.com/h"},
        )
        assert bad_url.is_error
        ok = await client.call_tool(
            "create_webhook_subscription",
            {"profile_id": 1, "name": "n", "trigger_on": "transfers#state-change", "url": "https://x.com/h"},
        )
        assert not ok.is_error
        assert (
            await client.call_tool("delete_webhook_subscription", {"profile_id": 1, "subscription_id": "s-1"})
        ).is_error
        assert not (
            await client.call_tool(
                "delete_webhook_subscription", {"profile_id": 1, "subscription_id": "s-1", "confirm": True}
            )
        ).is_error
        assert not (
            await client.call_tool(
                "create_recipient",
                {
                    "profile_id": 1,
                    "currency": "EUR",
                    "type": "iban",
                    "account_holder_name": "Bob B",
                    "details": {"iban": "DE89370400440532013000"},
                },
            )
        ).is_error
        assert not (await client.call_tool("delete_recipient", {"recipient_id": 5, "confirm": True})).is_error
        both = await client.call_tool(
            "create_quote", {"profile_id": 1, "source_currency": "EUR", "target_currency": "USD"}
        )
        assert both.is_error
        not_allowed = await client.call_tool(
            "create_quote",
            {
                "profile_id": 1,
                "source_currency": "EUR",
                "target_currency": "USD",
                "source_amount": 5,
                "target_account": 6,
            },
        )
        assert not_allowed.is_error and "WISE_ALLOWED_RECIPIENT_IDS" in not_allowed.content[0].text
        assert not (
            await client.call_tool(
                "create_quote",
                {"profile_id": 1, "source_currency": "EUR", "target_currency": "USD", "source_amount": 5},
            )
        ).is_error
        assert json.loads(quote.calls[0].request.content)["sourceAmount"] == 5
        assert not (
            await client.call_tool("update_quote", {"profile_id": 1, "quote_id": "q", "target_account": 5})
        ).is_error
        assert patch.calls[0].request.headers["content-type"] == "application/merge-patch+json"


# --- balance moves ----------------------------------------------------------------------------
async def test_balance_conversion_two_step_with_caps(make_server, mock_api) -> None:
    server, app = make_server(
        WISE_ENABLE="balance_moves", WISE_MAX_AMOUNT_PER_TX="EUR=100", WISE_MAX_AMOUNT_PER_DAY="EUR=150"
    )
    quote = {
        "id": "q-1",
        "sourceCurrency": "EUR",
        "sourceAmount": 80,
        "targetCurrency": "USD",
        "targetAmount": 90,
        "rate": 1.12,
    }
    mock_api.get("/v3/profiles/1/quotes/q-1").mock(return_value=httpx.Response(200, json=quote))
    move = mock_api.post("/v2/profiles/1/balance-movements").mock(
        return_value=httpx.Response(200, json={"id": 1, "state": "COMPLETED"})
    )
    async with Client(server) as client:
        no_token = await client.call_tool(
            "execute_balance_conversion", {"profile_id": 1, "quote_id": "q-1", "confirmation_token": "x" * 30}
        )
        assert no_token.is_error and not move.called
        prep = await client.call_tool("prepare_balance_conversion", {"profile_id": 1, "quote_id": "q-1"})
        token = prep.structured_content["confirmation_token"]
        assert prep.structured_content["summary"]["sell"] == "80 EUR"
        assert not move.called
        done = await client.call_tool(
            "execute_balance_conversion", {"profile_id": 1, "quote_id": "q-1", "confirmation_token": token}
        )
        assert not done.is_error, done.content
        assert json.loads(move.calls[0].request.content) == {"quoteId": "q-1"}
        assert move.calls[0].request.headers["x-idempotence-uuid"]
        replay = await client.call_tool(
            "execute_balance_conversion", {"profile_id": 1, "quote_id": "q-1", "confirmation_token": token}
        )
        assert replay.is_error and move.call_count == 1
        # Daily cap: 80 already used, another 80 would exceed 150.
        over = await client.call_tool("prepare_balance_conversion", {"profile_id": 1, "quote_id": "q-1"})
        assert over.is_error and "PER_DAY" in over.content[0].text
    assert any(e["event"] == "write_blocked" for e in audit_lines(app))


async def test_balance_move_param_binding(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE="balance_moves")
    mock_api.get("/v4/profiles/1/balances/2").mock(return_value=httpx.Response(200, json={"currency": "EUR"}))
    mock_api.get("/v4/profiles/1/balances/3").mock(
        return_value=httpx.Response(200, json={"currency": "EUR", "type": "SAVINGS", "name": "Taxes"})
    )
    move = mock_api.post("/v2/profiles/1/balance-movements").mock(return_value=httpx.Response(200, json={}))
    args = {"profile_id": 1, "source_balance_id": 2, "target_balance_id": 3, "amount": 25, "currency": "EUR"}
    async with Client(server) as client:
        token = (await client.call_tool("prepare_balance_move", args)).structured_content["confirmation_token"]
        tampered = await client.call_tool("execute_balance_move", {**args, "amount": 2500, "confirmation_token": token})
        assert tampered.is_error and not move.called
        token = (await client.call_tool("prepare_balance_move", args)).structured_content["confirmation_token"]
        ok = await client.call_tool("execute_balance_move", {**args, "confirmation_token": token})
        assert not ok.is_error, ok.content
        assert json.loads(move.calls[0].request.content)["amount"] == {"value": 25.0, "currency": "EUR"}


async def test_open_and_close_balance(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE="balance_moves")
    opened = mock_api.post("/v4/profiles/1/balances").mock(return_value=httpx.Response(200, json={"id": 4}))
    mock_api.delete("/v4/profiles/1/balances/4").mock(return_value=httpx.Response(200, json={}))
    async with Client(server) as client:
        assert (
            await client.call_tool("open_balance", {"profile_id": 1, "currency": "EUR", "type": "SAVINGS"})
        ).is_error
        assert not (
            await client.call_tool(
                "open_balance", {"profile_id": 1, "currency": "EUR", "type": "SAVINGS", "name": "Jar"}
            )
        ).is_error
        assert opened.calls[0].request.headers["x-idempotence-uuid"]
        assert (await client.call_tool("close_balance", {"profile_id": 1, "balance_id": 4})).is_error
        assert not (
            await client.call_tool("close_balance", {"profile_id": 1, "balance_id": 4, "confirm": True})
        ).is_error


# --- transfers & batches (only when explicitly enabled) ---------------------------------------
TRANSFER = {
    "id": 7,
    "targetAccount": 5,
    "sourceCurrency": "EUR",
    "sourceValue": 40,
    "targetValue": 45,
    "targetCurrency": "USD",
    "status": "incoming_payment_waiting",
    "details": {"reference": "r"},
}


async def test_transfer_lifecycle_when_enabled(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE=EVERYTHING, WISE_ALLOWED_RECIPIENT_IDS="5", WISE_MAX_AMOUNT_PER_TX="50")
    mock_api.get("/v1/transfers/7").mock(return_value=httpx.Response(200, json=TRANSFER))
    mock_api.post("/v1/transfer-requirements").mock(return_value=httpx.Response(200, json=[]))
    create = mock_api.post("/v1/transfers").mock(return_value=httpx.Response(200, json={"id": 7}))
    mock_api.put("/v1/transfers/7/cancel").mock(return_value=httpx.Response(200, json={"id": 7}))
    fund = mock_api.post("/v3/profiles/1/transfers/7/payments").mock(
        return_value=httpx.Response(200, json={"status": "COMPLETED"})
    )
    async with Client(server) as client:
        assert not (
            await client.call_tool("validate_transfer_requirements", {"target_account": 5, "quote_uuid": "q"})
        ).is_error
        assert (await client.call_tool("create_transfer", {"target_account": 6, "quote_uuid": "q"})).is_error
        assert not (
            await client.call_tool("create_transfer", {"target_account": 5, "quote_uuid": "q", "reference": "inv 1"})
        ).is_error
        assert json.loads(create.calls[0].request.content)["customerTransactionId"]
        prep = await client.call_tool("prepare_fund_transfer", {"profile_id": 1, "transfer_id": 7})
        token = prep.structured_content["confirmation_token"]
        assert not fund.called
        ok = await client.call_tool(
            "execute_fund_transfer", {"profile_id": 1, "transfer_id": 7, "confirmation_token": token}
        )
        assert not ok.is_error, ok.content
        assert json.loads(fund.calls[0].request.content) == {"type": "BALANCE"}
        assert (await client.call_tool("cancel_transfer", {"transfer_id": 7})).is_error
        assert not (await client.call_tool("cancel_transfer", {"transfer_id": 7, "confirm": True})).is_error


async def test_transfer_funding_respects_per_tx_cap(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE="transfers", WISE_MAX_AMOUNT_PER_TX="EUR=10")
    mock_api.get("/v1/transfers/7").mock(return_value=httpx.Response(200, json=TRANSFER))
    async with Client(server) as client:
        r = await client.call_tool("prepare_fund_transfer", {"profile_id": 1, "transfer_id": 7})
    assert r.is_error and "PER_TX" in r.content[0].text
    assert writes_sent(mock_api) == []


async def test_batch_lifecycle_when_enabled(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE="transfers,batch_transfers")
    group = {
        "id": "bg-1",
        "name": "Payroll",
        "sourceCurrency": "EUR",
        "transferIds": [7, 8],
        "status": "COMPLETED",
        "version": 2,
    }
    mock_api.get("/v3/profiles/1/batch-groups/bg-1").mock(return_value=httpx.Response(200, json=group))
    mock_api.get("/v1/transfers/7").mock(return_value=httpx.Response(200, json=TRANSFER))
    mock_api.get("/v1/transfers/8").mock(
        return_value=httpx.Response(200, json={**TRANSFER, "id": 8, "sourceValue": 2.5})
    )
    mock_api.post("/v3/profiles/1/batch-groups").mock(return_value=httpx.Response(200, json={"id": "bg-1"}))
    mock_api.post("/v3/profiles/1/batch-groups/bg-1/transfers").mock(return_value=httpx.Response(200, json={}))
    mock_api.patch("/v3/profiles/1/batch-groups/bg-1").mock(return_value=httpx.Response(200, json=group))
    fund = mock_api.post("/v3/profiles/1/batch-payments/bg-1/payments").mock(
        return_value=httpx.Response(200, json={"status": "COMPLETED"})
    )
    async with Client(server) as client:
        assert not (
            await client.call_tool("create_batch_group", {"profile_id": 1, "source_currency": "EUR", "name": "Payroll"})
        ).is_error
        assert not (
            await client.call_tool(
                "add_batch_transfer",
                {"profile_id": 1, "batch_group_id": "bg-1", "target_account": 5, "quote_uuid": "q"},
            )
        ).is_error
        assert not (
            await client.call_tool(
                "update_batch_group_status",
                {"profile_id": 1, "batch_group_id": "bg-1", "status": "COMPLETED", "version": 2, "confirm": True},
            )
        ).is_error
        prep = await client.call_tool("prepare_fund_batch_group", {"profile_id": 1, "batch_group_id": "bg-1"})
        assert prep.structured_content["amount"] == "42.5"
        ok = await client.call_tool(
            "execute_fund_batch_group",
            {
                "profile_id": 1,
                "batch_group_id": "bg-1",
                "confirmation_token": prep.structured_content["confirmation_token"],
            },
        )
        assert not ok.is_error and fund.called
