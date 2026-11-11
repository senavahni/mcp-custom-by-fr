"""MCP-V1-11 (integrasi): ingest PDF -> halaman, chunk bersitasi di books:<ws>, event outbox.

Embedder palsu 1024 dim (embed-local) — tanpa Gateway; ingest ulang harus 0 panggilan embedding.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import asyncpg
import numpy as np

from mcp_marketing.server.services import Services
from mcp_marketing.services.dashboard.outbox import Outbox
from mcp_marketing.services.kb.identity import EMBED_LOCAL_DIM, EMBED_LOCAL_MODEL
from mcp_marketing.services.knowledge.ingest import ingest_asset
from tests.integration.conftest import admin_test_url
from tests.pdf_factory import make_pdf, with_outline

WS = uuid.UUID("00000000-0000-4000-8000-000000000001")
TEXT = (
    "Segmentasi pasar membagi pelanggan menjadi kelompok dengan kebutuhan yang mirip. "
    "Targeting memilih segmen yang paling menarik. Positioning menempatkan merek."
)


class CountingEmbedder:
    model = EMBED_LOCAL_MODEL
    dim = EMBED_LOCAL_DIM

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            seed = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
            out.append(np.random.default_rng(seed).normal(size=self.dim).tolist())
        return out


async def _seed_asset(services: Services, tmp_path: Any) -> uuid.UUID:
    raw = make_pdf(["Pendahuluan\n" + TEXT, "", "Strategi\n" + TEXT * 3])
    data = with_outline(raw, [("Pendahuluan", 0, 0), ("Strategi STP", 2, 0)])
    path = tmp_path / "buku.pdf"
    path.write_bytes(data)
    conn = await asyncpg.connect(admin_test_url())
    try:
        upload_id = await conn.fetchval(
            "INSERT INTO mcp.uploads (workspace_id, sha256, kind, purpose, size_bytes, path, pages)"
            " VALUES ($1, $2, 'pdf', 'book', $3, $4, 3) RETURNING id",
            WS,
            hashlib.sha256(data).hexdigest(),
            len(data),
            str(path),
        )
        return await conn.fetchval(  # type: ignore[no-any-return]
            "INSERT INTO mcp.knowledge_assets (workspace_id, upload_id, kind, title, sha256, pages)"
            " VALUES ($1, $2, 'book', 'Dasar Marketing', $3, 3) RETURNING id",
            WS,
            upload_id,
            hashlib.sha256(data).hexdigest(),
        )
    finally:
        await conn.close()


async def test_ingest_pages_chunks_events_and_idempotent(services: Services, tmp_path: Any) -> None:
    services.outbox = Outbox(services.db, services.http, services.settings)
    asset_id = await _seed_asset(services, tmp_path)
    emb = CountingEmbedder()
    stats = await ingest_asset(services, asset_id, embedder=emb)
    assert stats["pages"] == 3 and stats["pages_needs_ocr"] == 1 and stats["structure"] == "outline"
    assert stats["chunks"] >= 2 and emb.calls >= 1

    conn = await asyncpg.connect(admin_test_url())
    try:
        status = await conn.fetchval(
            "SELECT status FROM mcp.knowledge_assets WHERE id=$1", asset_id
        )
        pages = await conn.fetch(
            "SELECT page_no, needs_ocr, chapter FROM mcp.knowledge_pages WHERE asset_id=$1 "
            "ORDER BY page_no",
            asset_id,
        )
        chunk_meta = await conn.fetch(
            "SELECT c.metadata, c.embed_dim, k.name FROM mcp.kb_chunks c "
            "JOIN mcp.kb_collections k ON k.id = c.collection_id ORDER BY c.chunk_index"
        )
        events = await conn.fetch(
            "SELECT stream, seq, payload->'data'->>'status' AS st FROM mcp.webhook_outbox "
            "WHERE stream = $1 ORDER BY seq",
            f"asset:{asset_id}",
        )
    finally:
        await conn.close()
    assert status == "ready"
    assert [(p["needs_ocr"], p["chapter"]) for p in pages] == [
        (False, "Pendahuluan"),
        (True, "Pendahuluan"),
        (False, "Strategi STP"),
    ]
    assert all(r["name"] == f"books:{WS}" and r["embed_dim"] == 1024 for r in chunk_meta)
    assert all('"page_start"' in r["metadata"] for r in chunk_meta)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["st"] == "extracting" and events[-1]["st"] == "ready"

    again = CountingEmbedder()
    stats2 = await ingest_asset(services, asset_id, embedder=again)
    assert stats2["skipped"] is True and again.calls == 0  # ingest ulang = 0 embedding
