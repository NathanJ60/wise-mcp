"""Defense in depth: even tools registered by mistake cannot reach denied endpoints."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp import Client

from tests.conftest import ALL_BUT_TRANSFERS, EVERYTHING, audit_lines, writes_sent
from wise_mcp.errors import PolicyError
from wise_mcp.policy import ROUTES, Scope

pytestmark = pytest.mark.anyio

TRANSFER = {"id": 77, "targetAccount": 5, "sourceCurrency": "EUR", "sourceValue": 10, "status": "x"}
GROUP = {"id": "bg-1", "sourceCurrency": "EUR", "transferIds": [77], "status": "COMPLETED", "version": 1}

MONEY_CALLS = [
    ("create_transfer", {"target_account": 5, "quote_uuid": "q-1"}),
    ("cancel_transfer", {"transfer_id": 77, "confirm": True}),
    ("validate_transfer_requirements", {"target_account": 5, "quote_uuid": "q-1"}),
    ("execute_fund_transfer", {"profile_id": 1, "transfer_id": 77, "confirmation_token": "x" * 20}),
    ("create_batch_group", {"profile_id": 1, "source_currency": "EUR", "name": "b"}),
    ("add_batch_transfer", {"profile_id": 1, "batch_group_id": "bg-1", "target_account": 5, "quote_uuid": "q"}),
    (
        "update_batch_group_status",
        {"profile_id": 1, "batch_group_id": "bg-1", "status": "COMPLETED", "version": 1, "confirm": True},
    ),
    ("execute_fund_batch_group", {"profile_id": 1, "batch_group_id": "bg-1", "confirmation_token": "x" * 20}),
]


def _mock_everything(router: respx.MockRouter) -> None:
    router.get("/v1/transfers/77").mock(return_value=httpx.Response(200, json=TRANSFER))
    router.get("/v3/profiles/1/batch-groups/bg-1").mock(return_value=httpx.Response(200, json=GROUP))
    router.route().mock(return_value=httpx.Response(200, json={"id": 1}))


@pytest.mark.parametrize("tool,args", MONEY_CALLS, ids=[c[0] for c in MONEY_CALLS])
async def test_deny_transfers_blocks_tools_registered_by_mistake(make_server, mock_api, tool, args) -> None:
    server, _app = make_server(enforce_scopes=False, WISE_ENABLE=EVERYTHING, WISE_DENY="transfers")
    _mock_everything(mock_api)
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error
    text = result.content[0].text
    assert "hard-denied" in text or "onfirmation token" in text, text
    assert writes_sent(mock_api) == []


async def test_prepare_tools_cannot_issue_usable_tokens_when_denied(make_server, mock_api) -> None:
    """Even if prepare succeeds (it only reads), execute is still blocked at the HTTP layer."""
    server, app = make_server(enforce_scopes=False, WISE_ENABLE=EVERYTHING, WISE_DENY="transfers")
    _mock_everything(mock_api)
    async with Client(server) as client:
        prep = await client.call_tool("prepare_fund_transfer", {"profile_id": 1, "transfer_id": 77})
        token = prep.structured_content["confirmation_token"]
        result = await client.call_tool(
            "execute_fund_transfer", {"profile_id": 1, "transfer_id": 77, "confirmation_token": token}
        )
    assert result.is_error and "hard-denied" in result.content[0].text
    assert writes_sent(mock_api) == []
    assert any(e["event"] == "write_blocked" for e in audit_lines(app))


@pytest.mark.parametrize(
    "route",
    [r for r in ROUTES if r.scope in (Scope.TRANSFERS, Scope.BATCH_TRANSFERS)],
    ids=lambda r: f"{r.method} {r.template}",
)
async def test_client_refuses_every_transfer_and_batch_route(make_server, mock_api, route) -> None:
    _, app = make_server(WISE_ENABLE=EVERYTHING, WISE_DENY="transfers")
    mock_api.route().mock(return_value=httpx.Response(200, json={}))
    path = route.template.replace("{p}", "1").replace("{id}", "2").replace("{uuid}", "u-1")
    with pytest.raises(PolicyError):
        await app.client.request(route.method, path, json={})
    assert mock_api.calls.call_count == 0


async def test_read_only_mode_blocks_all_writes_even_if_registered(make_server, mock_api) -> None:
    server, _ = make_server(enforce_scopes=False, WISE_ENABLE=EVERYTHING, WISE_READ_ONLY="true")
    _mock_everything(mock_api)
    calls = [
        (
            "create_recipient",
            {"profile_id": 1, "currency": "EUR", "type": "iban", "account_holder_name": "Ab", "details": {}},
        ),
        (
            "create_webhook_subscription",
            {"profile_id": 1, "name": "n", "trigger_on": "transfers#state-change", "url": "https://example.com/h"},
        ),
        ("open_balance", {"profile_id": 1, "currency": "EUR"}),
        ("publish_invoice", {"profile_id": 1, "payment_request_id": "pr-1", "confirm": True}),
        ("create_quote", {"profile_id": 1, "source_currency": "EUR", "target_currency": "USD", "source_amount": 5}),
        *MONEY_CALLS,
    ]
    async with Client(server) as client:
        for tool, args in calls:
            assert (await client.call_tool(tool, args)).is_error, tool
    assert writes_sent(mock_api) == []


async def test_nathan_config_allows_balance_moves_but_never_transfers(make_server, mock_api) -> None:
    server, _ = make_server(WISE_ENABLE=ALL_BUT_TRANSFERS, WISE_DENY="transfers,batch_transfers")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "prepare_balance_move" in names and "create_invoice_draft" in names
    assert not any("transfer" in n and not n.startswith(("list_", "get_")) for n in names)
    assert "create_batch_group" not in names
