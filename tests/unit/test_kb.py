"""TASK-19 unit: chunker (golden), potong teks, RRF, gabung chunk berurutan, embedder."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from mcp_marketing.core.errors import UpstreamError, ValidationFailed
from mcp_marketing.services.kb import embedder as emb
from mcp_marketing.services.kb.chunker import MIN_CHUNK_TOKENS, chunk_markdown
from mcp_marketing.services.kb.ingest import _posts_docs, text_doc
from mcp_marketing.services.kb.search import Hit, cut_text, merge_adjacent, rrf
from mcp_marketing.services.kb.sync import block_markdown, task_markdown
from mcp_marketing.services.kb.vector import cosine_scores
from mcp_marketing.tools.pm_agent import ContextHit, fuse
from mcp_marketing.utils.tokens import count_tokens

BRIEF = """# Brief Launch

Kopi gula aren untuk pasar urban.

## Target Audiens

Mahasiswa dan pekerja muda 18–30 tahun di Jabodetabek. Mereka aktif di TikTok dan Instagram,
peka harga, dan suka promo bundling. Mereka membeli kopi 3–4 kali seminggu.

## Kompetitor

| Brand | Harga | Kanal |
|---|---|---|
| Kopi A | 18rb | ojol |
| Kopi B | 22rb | gerai |

## Pesan Kunci

- Rasa gula aren asli
- Harga mulai 15 ribu

```
kode contoh tidak dipecah
baris kedua
```
"""


def test_chunker_breadcrumbs_and_blocks() -> None:
    chunks = chunk_markdown(BRIEF, title="Brief Kopi", chunk_size=800, overlap=100)
    joined = "\n".join(c.text for c in chunks)
    assert chunks[0].text.startswith("[Brief Kopi > Brief Launch]")
    assert "[Brief Kopi > Brief Launch > Target Audiens]" in joined
    assert "| Kopi A | 18rb | ojol |" in joined
    assert "kode contoh tidak dipecah\nbaris kedua" in joined
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.tokens == count_tokens(c.text) for c in chunks)


def test_chunker_splits_long_text_with_overlap_and_tables_by_row() -> None:
    long_par = " ".join(f"Kalimat nomor {i} tentang strategi kopi." for i in range(200))
    rows = "".join(f"| Segmen {i} | {i * 10} |\n" for i in range(80))
    md = f"## Panjang\n\n{long_par}\n\n## Tabel\n\n| Segmen | Nilai |\n|---|---|\n{rows}"
    chunks = chunk_markdown(md, chunk_size=200, overlap=40)
    assert all(c.tokens <= 200 + 20 for c in chunks)
    prose = [c for c in chunks if c.breadcrumb == "Panjang"]
    assert len(prose) > 3
    last_sentence = prose[0].text.rsplit("Kalimat", 1)[1]
    assert last_sentence in prose[1].text  # overlap membawa kalimat terakhir
    tables = [c for c in chunks if c.breadcrumb == "Tabel"]
    assert len(tables) > 1
    for chunk in tables:
        body = chunk.text.split("] ", 1)[1]
        assert body.startswith("| Segmen | Nilai |\n|---|---|")
        assert all(line.startswith("|") and line.endswith("|") for line in body.splitlines())


def test_chunker_merges_tiny_sections() -> None:
    md = "## A\n\nSatu.\n\n## B\n\n" + ("Isi panjang tentang kopi susu. " * 30)
    chunks = chunk_markdown(md, chunk_size=800, overlap=50)
    assert len(chunks) == 1 and "Satu." in chunks[0].text and "[B]" in chunks[0].text
    assert all(c.tokens >= MIN_CHUNK_TOKENS for c in chunks)


def test_chunker_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=50)
    with pytest.raises(ValueError):
        chunk_markdown("x", chunk_size=200, overlap=150)


def test_cut_text_sentence_boundary() -> None:
    text = "Kalimat pertama cukup panjang. " * 30
    cut = cut_text(text, 100)
    assert len(cut) <= 100 and cut.endswith(".")
    assert cut_text("pendek") == "pendek"
    assert cut_text("x" * 50 + " " + "y" * 80, 100).endswith("…")


def _hit(cid: str, doc: str = "d1", idx: int = 0, score: float = 1.0) -> Hit:
    return Hit(cid, doc, idx, "Judul", "text:x", f"teks {cid}", score)


def test_rrf_prefers_items_in_both_lists() -> None:
    fused = rrf([_hit("a"), _hit("b")], [_hit("b"), _hit("c")])
    assert [h.chunk_id for h in fused] == ["b", "a", "c"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61, rel=1e-4)


def test_merge_adjacent_chunks_same_document() -> None:
    merged = merge_adjacent(
        [_hit("a", "d1", 3, 0.9), _hit("b", "d1", 4, 0.5), _hit("c", "d2", 0, 0.7)]
    )
    assert [h.chunk_id for h in merged] == ["a", "c"]
    assert merged[0].text == "teks a\nteks b" and merged[0].score == 0.9


def test_cosine_scores() -> None:
    assert cosine_scores([1.0, 0.0], [[1.0, 0.0], [0.0, 2.0], [0.0, 0.0]]) == [1.0, 0.0, 0.0]
    assert cosine_scores([1.0], []) == []


def test_mean_pool_normalize() -> None:
    hidden = np.array([[[1.0, 0.0], [3.0, 0.0], [100.0, 100.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0]], dtype=np.int64)
    assert emb.mean_pool_normalize(hidden, mask) == [[1.0, 0.0]]


async def test_local_embedder_requires_download(tmp_path: Path) -> None:
    with pytest.raises(UpstreamError, match="download-models"):
        await emb.LocalEmbedder(tmp_path).embed(["halo"], "query")


def test_embedder_for_selection(tmp_path: Path) -> None:
    services = SimpleNamespace(
        extras={},
        llm=object(),
        settings=SimpleNamespace(nlp=SimpleNamespace(local_embed_model_dir=tmp_path)),
    )
    assert isinstance(emb.embedder_for(services, "embed-default", 1536), emb.GatewayEmbedder)
    local = emb.embedder_for(services, "local/multilingual-e5-small", 384)
    assert local is emb.embedder_for(services, "local/multilingual-e5-small", 384)
    with pytest.raises(ValidationFailed):
        emb.embedder_for(services, "lain", 10)
    services.extras["kb_embedder"] = "fake"
    assert emb.embedder_for(services, "x", 1) == "fake"


async def test_gateway_embedder_checks_dimension() -> None:
    class LLM:
        async def embed(self, texts: list[str], budget: Any = None) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    with pytest.raises(ValidationFailed):
        await emb.GatewayEmbedder(LLM(), 1536).embed(["a"], "passage")
    assert await emb.GatewayEmbedder(LLM(), 2).embed([], "query") == []


def test_dashboard_markdown() -> None:
    def block(kind: str, text: str, **props: Any) -> Any:
        return SimpleNamespace(block_type=kind, plain_text=text, content_json={"props": props})

    assert block_markdown(block("heading", "Tujuan", level=1)) == "# Tujuan"
    assert block_markdown(block("checkListItem", "Riset", checked=True)) == "- [x] Riset"
    assert block_markdown(block("bulletListItem", "Poin")) == "- Poin"
    assert block_markdown(block("paragraph", "  ")) == ""
    task = SimpleNamespace(
        title="Konten minggu 1",
        status="todo",
        priority="high",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 6),
        tags=["instagram"],
    )
    md = task_markdown(task, [block("paragraph", "Buat 3 reels")])
    assert md.startswith("# Konten minggu 1\n\nStatus: todo | Prioritas: high")
    assert "Tag: instagram" in md and md.endswith("Buat 3 reels")


def test_social_posts_grouped_per_day_keyword() -> None:
    rows = [
        {"posted_at": "2026-10-01T03:00:00+00:00", "keywords": ["kopi"], "text": "a", "likes": 1},
        {"posted_at": "2026-10-01T05:00:00+00:00", "keywords": ["kopi"], "text": "b", "likes": 9},
        {"posted_at": "2026-10-02T05:00:00+00:00", "keywords": ["kopi", "teh"], "text": "c"},
    ]
    docs = _posts_docs("h_abcdefghij", rows, {})
    assert [d.source_ref for d in docs] == [
        "h_abcdefghij:2026-10-01:kopi",
        "h_abcdefghij:2026-10-02:kopi",
        "h_abcdefghij:2026-10-02:teh",
    ]
    assert docs[0].content.index("): b") < docs[0].content.index("): a")  # urut likes


def test_text_doc_reference_and_title() -> None:
    doc = text_doc("# Judul Dokumen\n\nisi", {})
    assert doc.title == "Judul Dokumen" and len(doc.source_ref) == 32
    assert text_doc("isi", {"ref": "brief-1"}).source_ref == "brief-1"
    with pytest.raises(ValidationFailed):
        text_doc("  ", {})


def test_pm_context_fuse_dedupes_same_text() -> None:
    dash = [ContextHit(block_id="b1", text="Target audiens mahasiswa"), ContextHit(text="lain")]
    kb = [ContextHit(source="kb:x", text="target audiens   MAHASISWA"), ContextHit(text="baru")]
    fused = fuse(dash, kb, 3)
    assert fused[0].block_id == "b1" and len(fused) == 3
    assert fused[0].rank == pytest.approx(2 / 61, rel=1e-3)
