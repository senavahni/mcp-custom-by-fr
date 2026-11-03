"""Workspace efektif pemanggil tool: workspace run (bila dalam run) atau workspace klien."""

from __future__ import annotations

import uuid

from mcp_marketing.core.errors import Forbidden
from mcp_marketing.models.runs import Run
from mcp_marketing.server.registry import ToolContext
from mcp_marketing.services.db.query import WorkspaceScope


async def workspace_scope(ctx: ToolContext) -> WorkspaceScope:
    workspace: str | None = ctx.client.workspace_id
    if ctx.run_id:
        async with ctx.services.db.session() as session:
            run = await session.get(Run, uuid.UUID(ctx.run_id))
        if run is None:
            raise Forbidden("Run tidak dikenal")
        workspace = str(run.workspace_id) if run.workspace_id else workspace
    try:
        ws = uuid.UUID(workspace) if workspace else None
    except ValueError:
        raise Forbidden("Workspace klien tidak valid") from None
    return WorkspaceScope(workspace_id=ws, is_admin=ctx.client.is_admin)
