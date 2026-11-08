"""Server assembly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer

from wise_mcp import __version__
from wise_mcp.config import Settings
from wise_mcp.tools import register_all
from wise_mcp.tools.common import App, Registrar

INSTRUCTIONS = """\
Wise (wise.com) account access. Permissions are scope-gated by the server operator: tools that
are not listed are disabled on purpose and cannot be enabled from the conversation.
Call get_permissions to see the effective policy. Money-moving operations are two-step:
call the prepare_* tool, show the summary to the user, and only after their explicit approval
call the matching execute_* tool with the confirmation_token. Never invent ids: use the list_* tools.
"""


def build_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    enforce_scopes: bool = True,
) -> tuple[MCPServer, App]:
    """Build the MCP server. ``enforce_scopes=False`` exists only for tests that prove the HTTP
    layer still blocks a tool registered by mistake."""
    app = App.create(settings, transport=transport)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await app.client.aclose()

    server = MCPServer(
        name="wise-mcp",
        title="Wise",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/NathanJ60/wise-mcp",
        lifespan=lifespan,
    )
    register_all(Registrar(server, app, enforce_scopes=enforce_scopes))
    return server, app
