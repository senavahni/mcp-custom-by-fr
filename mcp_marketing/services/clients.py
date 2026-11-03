"""Manajemen klien MCP (API key `mcpk_…`): buat, daftar, cabut, rotasi.

Key lengkap hanya dikembalikan sekali saat dibuat/dirotasi; DB menyimpan HMAC(pepper, key).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_marketing.core.errors import Conflict, NotFound, ValidationFailed
from mcp_marketing.models import ApiClient
from mcp_marketing.utils import crypto
from mcp_marketing.utils.time import now

SYSTEM_DASHBOARD = "dashboard"
SYSTEM_KEY_PREFIX = "sysdash0"
# klien sistem diautentikasi via MCP_SERVER_API_KEY; hash ini tidak bisa dicocokkan key mana pun
SYSTEM_KEY_HASH = "0" * 64


@dataclass(frozen=True)
class CreatedClient:
    client: ApiClient
    key: str


def client_summary(c: ApiClient) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "kind": c.kind,
        "key_prefix": c.key_prefix,
        "toolsets": list(c.toolsets),
        "is_admin": c.is_admin,
        "status": c.status,
        "rpm_limit": c.rpm_limit,
        "daily_token_budget": c.daily_token_budget,
        "workspace_id": str(c.workspace_id) if c.workspace_id else None,
        "dashboard_agent_id": str(c.dashboard_agent_id) if c.dashboard_agent_id else None,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
    }


async def _unique_prefix(session: AsyncSession) -> tuple[str, str]:
    for _ in range(5):
        key, prefix = crypto.generate_client_key()
        exists = await session.scalar(select(ApiClient.id).where(ApiClient.key_prefix == prefix))
        if exists is None:
            return key, prefix
    raise Conflict("Gagal membuat prefix key unik, coba lagi")


async def create_client(
    session: AsyncSession,
    *,
    pepper: str,
    name: str,
    toolsets: list[str],
    kind: str = "external",
    is_admin: bool = False,
    workspace_id: uuid.UUID | None = None,
    dashboard_agent_id: uuid.UUID | None = None,
    rpm_limit: int = 120,
    daily_token_budget: int | None = None,
    expires_at: datetime | None = None,
) -> CreatedClient:
    if not name.strip():
        raise ValidationFailed("name wajib diisi")
    if await session.scalar(select(ApiClient.id).where(ApiClient.name == name)):
        raise Conflict(f"Klien '{name}' sudah ada")
    key, prefix = await _unique_prefix(session)
    client = ApiClient(
        name=name,
        kind=kind,
        key_prefix=prefix,
        key_hash=crypto.hash_client_key(pepper, key),
        toolsets=sorted(set(toolsets)),
        is_admin=is_admin,
        workspace_id=workspace_id,
        dashboard_agent_id=dashboard_agent_id,
        rpm_limit=rpm_limit,
        daily_token_budget=daily_token_budget,
        expires_at=expires_at,
    )
    session.add(client)
    await session.flush()
    return CreatedClient(client=client, key=key)


async def list_clients(session: AsyncSession) -> list[ApiClient]:
    return list((await session.scalars(select(ApiClient).order_by(ApiClient.created_at))).all())


async def _get(session: AsyncSession, ident: str) -> ApiClient:
    try:
        cond = ApiClient.id == uuid.UUID(ident)
    except ValueError:
        cond = ApiClient.name == ident
    client = await session.scalar(select(ApiClient).where(cond))
    if client is None:
        raise NotFound(f"Klien tidak ditemukan: {ident}")
    return client


async def revoke_client(session: AsyncSession, ident: str) -> ApiClient:
    client = await _get(session, ident)
    await session.execute(
        update(ApiClient)
        .where(ApiClient.id == client.id)
        .values(status="revoked", updated_at=now())
    )
    await session.refresh(client)
    return client


async def rotate_client(session: AsyncSession, ident: str, *, pepper: str) -> CreatedClient:
    client = await _get(session, ident)
    if client.kind == "system":
        raise ValidationFailed("Klien sistem tidak memakai key mcpk_ (pakai MCP_SERVER_API_KEY)")
    key, prefix = await _unique_prefix(session)
    client.key_prefix = prefix
    client.key_hash = crypto.hash_client_key(pepper, key)
    client.status = "active"
    client.updated_at = now()
    await session.flush()
    return CreatedClient(client=client, key=key)
