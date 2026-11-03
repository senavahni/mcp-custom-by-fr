"""Penyimpanan embedding yang berjalan dengan atau tanpa pgvector.

- Tulis selalu `CAST(:e AS real[])`: kolom `vector` menerimanya lewat cast assignment pgvector,
  kolom `real[]` (mode cadangan) menyimpannya apa adanya.
- Baca selalu `embedding::real[]` (cast `vector -> real[]` disediakan pgvector).
- Pencarian ANN memakai operator `<=>` + indeks HNSW hanya bila kolom bertipe `vector`.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from mcp_marketing.core.logging import get_logger

log = get_logger(__name__)

Mode = Literal["vector", "array"]
HNSW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS kb_chunks_hnsw_1536 ON mcp.kb_chunks "
    "USING hnsw ((embedding::vector(1536)) vector_cosine_ops) WHERE embed_dim = 1536",
    "CREATE INDEX IF NOT EXISTS kb_chunks_hnsw_384 ON mcp.kb_chunks "
    "USING hnsw ((embedding::vector(384)) vector_cosine_ops) WHERE embed_dim = 384",
)
_COLUMN_TYPE_SQL = (
    "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
    "WHERE a.attrelid = to_regclass(:t) AND a.attname = 'embedding' AND NOT a.attisdropped"
)
_mode_cache: dict[str, Mode] = {}


async def vector_mode(session: AsyncSession | AsyncConnection) -> Mode:
    """`vector` bila `kb_chunks.embedding` bertipe pgvector, selain itu `array`."""
    cached = _mode_cache.get("kb_chunks")
    if cached:
        return cached
    kind = (await session.execute(text(_COLUMN_TYPE_SQL), {"t": "mcp.kb_chunks"})).scalar()
    mode: Mode = "vector" if kind and str(kind).startswith("vector") else "array"
    _mode_cache["kb_chunks"] = mode
    return mode


def reset_mode_cache() -> None:
    _mode_cache.clear()


async def promote_to_pgvector(conn: AsyncConnection) -> bool:
    """Ubah kolom `real[]` -> `vector` + buat HNSW bila ekstensi tersedia (idempoten)."""
    has_ext = (
        await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    ).scalar()
    if not has_ext:
        return False
    changed = False
    for table in ("mcp.kb_chunks", "mcp.embedding_cache"):
        kind = (await conn.execute(text(_COLUMN_TYPE_SQL), {"t": table})).scalar()
        if kind and str(kind).startswith("real"):
            await conn.execute(
                text(
                    f"ALTER TABLE {table} ALTER COLUMN embedding "
                    "TYPE vector USING embedding::vector"
                )
            )
            changed = True
    for statement in HNSW_INDEXES:
        await conn.execute(text(statement))
    reset_mode_cache()
    if changed:
        log.info("kb.promoted_to_pgvector")
    return changed


def cosine_scores(query: list[float], matrix: list[list[float]]) -> list[float]:
    """Kemiripan kosinus (mode cadangan tanpa pgvector)."""
    if not matrix:
        return []
    q = np.asarray(query, dtype=np.float32)
    m = np.asarray(matrix, dtype=np.float32)
    denom = np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1.0)
    denom[denom == 0] = 1.0
    return [float(x) for x in (m @ q) / denom]


def as_float_list(value: Any) -> list[float]:
    return [float(x) for x in value]
