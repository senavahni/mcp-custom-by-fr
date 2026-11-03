"""Server MCP low-level (SDK `mcp` 2.x): tools/list terfilter per klien, tools/call via pipeline.

Catatan spike (CHANGELOG 0.1.0-dev): SDK 2.x memakai `Server(on_list_tools=…, on_call_tool=…)`;
`FastMCP` 1.x diganti `MCPServer` yang membangun schema dari signature fungsi, sehingga untuk
visibilitas per-klien dan schema Pydantic eksplisit dipakai server low-level.
"""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server

from mcp_marketing import SERVER_NAME, __version__
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.visibility import parse_toolsets, visible_tools

INSTRUCTIONS = (
    "MCP Marketing: tool marketing hemat token. Mulai dengan toolbox_list untuk melihat toolset. "
    "Data besar dikembalikan sebagai handle (h_…); baca dengan data_get/data_aggregate."
)


def _state(ctx: ServerRequestContext[Any, Any]) -> dict[str, Any]:
    request = ctx.request
    scope = getattr(request, "scope", None) or {}
    return scope.get("state") or {}  # type: ignore[no-any-return]


def _client(ctx: ServerRequestContext[Any, Any]) -> ClientIdentity:
    client = _state(ctx).get("client")
    if not isinstance(client, ClientIdentity):
        # AuthMiddleware selalu mengisi; tanpa klien tidak ada tool yang terlihat
        return ClientIdentity(id=None, name="anonymous", kind="external", toolsets=frozenset())
    return client


def build_mcp_server(pipeline: ToolPipeline) -> Server[Any]:
    settings = pipeline.services.settings

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        client = _client(ctx)
        request = ctx.request
        narrow = parse_toolsets(
            request.query_params.get("toolsets") if request is not None else None
        )
        tools = [
            types.Tool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema(),
                output_schema=spec.output_schema(),
            )
            for spec in visible_tools(client, settings, narrow)
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        state = _state(ctx)
        outcome = await pipeline.call(
            params.name,
            params.arguments,
            client=_client(ctx),
            request_id=state.get("request_id"),
        )
        return types.CallToolResult(
            content=[types.TextContent(text=outcome.text)],
            structured_content=outcome.structured,
            is_error=outcome.is_error,
        )

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
