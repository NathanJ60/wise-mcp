from __future__ import annotations

import pytest
from mcp import Client

from tests.conftest import EVERYTHING

pytestmark = pytest.mark.anyio

WRITE_TOOLS = {
    "create_invoice_draft",
    "update_invoice_draft",
    "publish_invoice",
    "cancel_invoice",
    "create_webhook_subscription",
    "delete_webhook_subscription",
    "create_recipient",
    "delete_recipient",
    "create_quote",
    "update_quote",
    "validate_transfer_requirements",
    "create_transfer",
    "cancel_transfer",
    "prepare_fund_transfer",
    "execute_fund_transfer",
    "create_batch_group",
    "add_batch_transfer",
    "update_batch_group_status",
    "prepare_fund_batch_group",
    "execute_fund_batch_group",
    "open_balance",
    "close_balance",
    "prepare_balance_conversion",
    "execute_balance_conversion",
    "prepare_balance_move",
    "execute_balance_move",
}


async def _tools(server):  # type: ignore[no-untyped-def]
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def test_default_registers_only_read_tools(make_server) -> None:
    server, app = make_server()
    tools = await _tools(server)
    assert tools, "no tools registered"
    assert not (set(tools) & WRITE_TOOLS)
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert set(app.skipped_tools) == WRITE_TOOLS


async def test_everything_enabled_registers_all(make_server) -> None:
    server, _ = make_server(WISE_ENABLE=EVERYTHING)
    tools = await _tools(server)
    assert set(tools) >= WRITE_TOOLS


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("invoices", {"create_invoice_draft", "update_invoice_draft", "publish_invoice", "cancel_invoice"}),
        ("webhooks", {"create_webhook_subscription", "delete_webhook_subscription"}),
        ("recipients", {"create_recipient", "delete_recipient"}),
        ("quotes", {"create_quote", "update_quote"}),
    ],
)
async def test_single_scope(make_server, scope: str, expected: set[str]) -> None:
    server, _ = make_server(WISE_ENABLE=scope)
    assert set(await _tools(server)) & WRITE_TOOLS == expected


async def test_annotations_are_consistent(make_server) -> None:
    server, _ = make_server(WISE_ENABLE=EVERYTHING)
    for name, tool in (await _tools(server)).items():
        a = tool.annotations
        assert a is not None and a.title, name
        assert tool.output_schema is not None, name
        if name.startswith(("list_", "get_", "prepare_", "compare_")) or name == "validate_transfer_requirements":
            assert a.read_only_hint is True, name
        else:
            assert a.read_only_hint is False, name
        if name.startswith(("execute_", "delete_", "close_", "cancel_")) or name == "publish_invoice":
            assert a.destructive_hint is True, name
        if name.startswith("create_"):
            assert a.destructive_hint is False, name


async def test_tool_input_validation(make_server) -> None:
    server, _ = make_server()
    async with Client(server) as client:
        r = await client.call_tool("get_exchange_rate", {"source": "eur", "target": "USD"})
        assert r.is_error
        r = await client.call_tool("list_balances", {"profile_id": -1})
        assert r.is_error
