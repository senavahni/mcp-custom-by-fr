"""Toolset `intel`: rotasi key LLM Gateway per agent dari Dashboard (M3_TASK_009 P1).

Key baru dibuat lewat Admin API Gateway (mekanisme sama dengan `manage.py gateway-keys provision`),
disimpan terenkripsi, lalu key lama dicabut. Respons hanya memuat prefix, tidak pernah key lengkap.
Dashboard melakukan RBAC (Admin/Owner); admin key Gateway dibaca dari `LLM_GATEWAY_ADMIN_KEY`.
"""

from __future__ import annotations

from typing import Annotated

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from mcp_marketing.core.errors import Conflict, UnknownAgent, UpstreamError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentProfile
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.llm.credentials import SYSTEM
from mcp_marketing.services.llm.gateway_admin import GatewayAdmin, admin_base_url, rotate_credential

log = get_logger(__name__)

TOOLSET = "intel"
ROTATE_AGENT_KEY_PATTERN = r"^[a-z0-9_-]{2,64}$"
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

RotateAgentKey = Annotated[str, StringConstraints(pattern=ROTATE_AGENT_KEY_PATTERN)]


class LlmKeyRotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: RotateAgentKey


class LlmKeyRotateOut(BaseModel):
    agent_key: str
    key_prefix: str
    rotated_at: AwareDatetime
    old_key_revoked: bool


@tool(
    name="intel_llm_key_rotate",
    toolset=TOOLSET,
    summary="Rotasi key LLM Gateway milik satu agent (key lama dicabut)",
    description="Create a new LLM Gateway key for an agent via the Admin API, store it encrypted "
    "and revoke the old one. Returns only the key prefix, never the full key.",
    input_model=LlmKeyRotateIn,
    output_model=LlmKeyRotateOut,
    machine_output=True,
    writes=True,
    timeout_s=60,
)
async def intel_llm_key_rotate(inp: LlmKeyRotateIn, ctx: ToolContext) -> LlmKeyRotateOut:
    secret = ctx.settings.mcp_secret_key
    if secret is None:
        raise Conflict("MCP_SECRET_KEY belum diisi di .env MCP; key per agent belum aktif.")
    admin_key = ctx.settings.llm.llm_gateway_admin_key
    if admin_key is None:
        raise Conflict(
            "LLM_GATEWAY_ADMIN_KEY belum diisi di .env MCP; rotasi dari Dashboard belum aktif."
        )
    if inp.agent_key != SYSTEM:
        async with ctx.services.db.session() as session:
            known = await session.scalar(
                select(AgentProfile.agent_key).where(AgentProfile.agent_key == inp.agent_key)
            )
        if known is None:
            raise UnknownAgent(f"Agent {inp.agent_key} tidak dikenal di MCP")
    gateway_url = ctx.settings.llm.llm_gateway_url
    admin = GatewayAdmin(
        ctx.services.http, admin_base_url(gateway_url), admin_key.get_secret_value().strip()
    )
    try:
        rotated = await rotate_credential(
            ctx.services.db,
            secret.get_secret_value(),
            admin,
            agent_key=None if inp.agent_key == SYSTEM else inp.agent_key,
        )
    except httpx.HTTPError:
        raise UpstreamError(
            "LLM Gateway tidak bisa dihubungi — pastikan Gateway berjalan.", retryable=True
        ) from None
    except UpstreamError as err:
        if (err.details or {}).get("status") in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            raise UpstreamError(
                "Admin key Gateway ditolak (bukan admin / sudah dicabut).", retryable=False
            ) from None
        raise
    store = getattr(ctx.services.llm, "credentials", None)
    if store is not None:
        store.invalidate()  # server langsung memakai key baru; worker menyusul (cache 60 s)
    log.info("llm.key_rotated", agent_key=inp.agent_key, prefix=rotated.prefix)
    return LlmKeyRotateOut(
        agent_key=inp.agent_key,
        key_prefix=rotated.prefix,
        rotated_at=rotated.rotated_at,
        old_key_revoked=rotated.old_key_revoked,
    )
