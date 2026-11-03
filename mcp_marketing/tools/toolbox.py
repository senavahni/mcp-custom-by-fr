"""Toolset `toolbox`: daftar toolset tanpa schema (progressive disclosure, MCP_SPEC §1.3)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from mcp_marketing.schemas.common import ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.server.visibility import TOOLSET_DESCRIPTIONS, visible_tools


class ToolboxListIn(ToolInput):
    toolset: str | None = Field(default=None, description="Batasi ke satu toolset")


class ToolBrief(BaseModel):
    name: str
    summary: str


class ToolsetOut(BaseModel):
    name: str
    description: str
    tools: list[ToolBrief]


class ToolboxListOut(BaseModel):
    toolsets: list[ToolsetOut]


@tool(
    name="toolbox_list",
    toolset="toolbox",
    summary="Daftar toolset dan tool yang tersedia untuk klien ini",
    description="List toolsets and tools visible to this API key (one-line summary each, "
    "no schemas). "
    "Call first to choose a toolset before loading full tool schemas.",
    input_model=ToolboxListIn,
    output_model=ToolboxListOut,
)
async def toolbox_list(inp: ToolboxListIn, ctx: ToolContext) -> ToolboxListOut:
    narrow = frozenset({inp.toolset}) if inp.toolset else None
    grouped: dict[str, list[ToolBrief]] = {}
    for spec in visible_tools(ctx.client, ctx.settings, narrow):
        grouped.setdefault(spec.toolset, []).append(ToolBrief(name=spec.name, summary=spec.summary))
    return ToolboxListOut(
        toolsets=[
            ToolsetOut(name=name, description=TOOLSET_DESCRIPTIONS.get(name, ""), tools=tools)
            for name, tools in sorted(grouped.items())
        ]
    )


class PingIn(ToolInput):
    message: str = Field(default="ping", max_length=200)


class PingOut(BaseModel):
    summary: str
    pong: bool = True
    client: str


@tool(
    name="toolbox_ping",
    toolset="toolbox",
    summary="Tes koneksi (dev)",
    description="Development-only connectivity check; echoes the message.",
    input_model=PingIn,
    output_model=PingOut,
    dev_only=True,
)
async def toolbox_ping(inp: PingIn, ctx: ToolContext) -> PingOut:
    return PingOut(summary=f"pong: {inp.message}", client=ctx.client.name)
