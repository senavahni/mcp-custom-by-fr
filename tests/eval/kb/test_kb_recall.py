"""Eval retrieval KB (TASK-19/26): recall@6 `hybrid` vs `keyword` atas 3 brief + 30 pertanyaan.

Default memakai `HashEmbedder` (baseline deterministik, tanpa biaya). Set `KB_EVAL_LIVE=1` untuk
embedding Gateway nyata; ambang AC (recall@6 hybrid >= 0,85) hanya ditegakkan di mode live.

    scripts\\test.bat tests/eval/kb -m eval
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_marketing.models.kb import KbCollection
from mcp_marketing.server.services import Services
from mcp_marketing.services.kb.embedder import Embedder, GatewayEmbedder
from mcp_marketing.services.kb.search import search
from mcp_marketing.services.kb.store import DocInput, upsert_document
from tests.fakes import HashEmbedder

pytestmark = [pytest.mark.eval, pytest.mark.integration]
HERE = Path(__file__).parent
TOP_K = 6
LIVE = os.environ.get("KB_EVAL_LIVE") == "1"
RECALL_TARGET = 0.85
KEYWORD_FLOOR = 0.6


def _questions() -> list[dict[str, str]]:
    lines = (HERE / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def _recall(services: Services, coll: KbCollection, embedder: Embedder, mode: str) -> float:
    hits_ok = 0
    questions = _questions()
    for item in questions:
        hits = await search(
            services.db,
            [coll.id],
            item["q"],
            mode=mode,  # type: ignore[arg-type]
            top_k=TOP_K,
            embedder=embedder,
            dim=coll.embed_dim,
        )
        if any(item["expect"].lower() in h.text.lower() for h in hits):
            hits_ok += 1
    return hits_ok / len(questions)


async def test_kb_recall_at_6(services: Services) -> None:
    if LIVE:
        assert services.llm is not None, "mode live butuh LLM Gateway"
        embedder: Embedder = GatewayEmbedder(services.llm, services.settings.nlp.embed_dim)
    else:
        embedder = HashEmbedder()
    async with services.db.session() as session:
        coll = KbCollection(name="eval-kb", embed_model=embedder.model, embed_dim=embedder.dim)
        session.add(coll)
        await session.flush()
    for path in sorted((HERE / "briefs").glob("*.md")):
        body = path.read_text(encoding="utf-8")
        await upsert_document(
            services.db,
            coll,
            DocInput("text", path.stem, path.stem, body, {}),
            embedder,
            chunk_size=200,
            overlap=40,
        )
    hybrid = await _recall(services, coll, embedder, "hybrid")
    keyword = await _recall(services, coll, embedder, "keyword")
    print(f"recall@{TOP_K}: hybrid={hybrid:.2f} keyword={keyword:.2f} live={LIVE}")
    # regresi keyword: pertanyaan kalimat utuh harus tetap cocok sebagian (bukan AND semua kata)
    assert keyword >= KEYWORD_FLOOR
    if LIVE:
        # HashEmbedder tidak semantik, jadi perbandingan ini hanya bermakna dengan embedding nyata
        assert hybrid >= keyword
        assert hybrid >= RECALL_TARGET
