"""Cache hasil tool & LLM di `mcp.cache_entries` (MCP_SPEC §3 T7).

Kunci tool = sha256(tool | versi | args kanonik | workspace). `response_format` dan `cache`
tidak ikut kunci: yang disimpan adalah output handler SEBELUM kompaksi, sehingga mode concise
dan detailed berbagi entri yang sama. `limit`/`cursor` tetap ikut kunci karena mengubah hasil.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import orjson
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert

from mcp_marketing.core.database import Database
from mcp_marketing.models import CacheEntry
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.time import now

EXCLUDED_ARGS = frozenset({"response_format", "cache"})


def canonical_json(value: Any) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS, default=str).decode()


def tool_cache_key(tool: str, version: int, args: dict[str, Any], workspace_id: str | None) -> str:
    material = {k: v for k, v in args.items() if k not in EXCLUDED_ARGS}
    return sha256_hex(f"{tool}|{version}|{canonical_json(material)}|{workspace_id or '-'}")


def llm_cache_key(route: str, messages: list[dict[str, Any]], params: dict[str, Any]) -> str:
    return sha256_hex(f"{route}|{canonical_json(messages)}|{canonical_json(params)}")


class ResultCache:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE mcp.cache_entries SET hits = hits + 1 "
                        "WHERE cache_key = :k AND expires_at > now() RETURNING value"
                    ),
                    {"k": key},
                )
            ).first()
        return dict(row[0]) if row else None

    async def set(self, key: str, namespace: str, value: dict[str, Any], ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        payload = orjson.loads(orjson.dumps(value, default=str))
        size = len(orjson.dumps(payload))
        expires = now() + timedelta(seconds=ttl_s)
        stmt = insert(CacheEntry).values(
            cache_key=key, namespace=namespace, value=payload, size_bytes=size, expires_at=expires
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["cache_key"],
            set_={"value": payload, "size_bytes": size, "expires_at": expires, "hits": 0},
        )
        async with self.db.session() as session:
            await session.execute(stmt)

    async def purge_expired(self) -> int:
        async with self.db.session() as session:
            result = await session.execute(delete(CacheEntry).where(CacheEntry.expires_at <= now()))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
