"""Pencarian KB: `vector`, `keyword` (tsvector), `hybrid` (Reciprocal Rank Fusion, k=60).

Vector memakai indeks HNSW (`embedding::vector(D) <=> …`) bila pgvector aktif; mode cadangan
menghitung kosinus di aplikasi atas ≤ BRUTE_FORCE_MAX chunk terbaru koleksi.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

import orjson
from sqlalchemy import text

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.kb.vector import as_float_list, cosine_scores, vector_mode

log = get_logger(__name__)

Mode = Literal["hybrid", "vector", "keyword"]
CANDIDATES = 30
RRF_K = 60
TEXT_MAX_CHARS = 600
BRUTE_FORCE_MAX = 20_000
ALLOWED_DIMS = frozenset({384, 768, 1024, 1536})
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    chunk_index: int
    document_title: str | None
    source: str
    text: str
    score: float


def cut_text(value: str, limit: int = TEXT_MAX_CHARS) -> str:
    """Potong di batas kalimat terakhir sebelum `limit` (atau kata bila tidak ada)."""
    value = value.strip()
    if len(value) <= limit:
        return value
    head = value[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= limit // 2:
        return head[: ends[-1]].rstrip()
    return head.rsplit(" ", 1)[0].rstrip() + "…"


_WORD = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_TERMS = 32
STOPWORDS = frozenset(
    "yang dan di ke dari untuk dengan pada ini itu apa siapa berapa bagaimana kapan mana "
    "adalah atau juga saja akan sudah bisa ada tidak dalam oleh para serta agar the a an of "
    "to in for on and or is are what how who".split()
)


def keyword_query(query: str) -> str:
    """Pertanyaan -> `kata1 or kata2 …` (websearch syntax): cocok sebagian, diranking ts_rank.

    `websearch_to_tsquery` default meng-AND semua kata sehingga pertanyaan kalimat utuh hampir
    tidak pernah cocok; stopword dibuang agar tidak mendominasi ranking.
    """
    terms = [w for w in _WORD.findall(query.lower()) if w not in STOPWORDS and len(w) > 1]
    return " or ".join(dict.fromkeys(terms[:MAX_QUERY_TERMS]))


def _filter_sql(filters: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    if not filters:
        return "", {}
    return " AND c.metadata @> CAST(:flt AS jsonb)", {"flt": orjson.dumps(filters).decode()}


_SELECT = (
    "SELECT c.id, c.document_id, c.chunk_index, c.text, d.title, d.source_type, d.source_ref "
    "FROM mcp.kb_chunks c JOIN mcp.kb_documents d ON d.id = c.document_id "
)


async def keyword_hits(
    session: Any, collection_ids: list[uuid.UUID], query: str, filters: dict[str, Any] | None
) -> list[Hit]:
    terms = keyword_query(query)
    if not terms:
        return []
    flt, params = _filter_sql(filters)
    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.document_id, c.chunk_index, c.text, d.title, d.source_type, "
                "d.source_ref, ts_rank(c.text_tsv, websearch_to_tsquery('simple', :q)) AS score "
                "FROM mcp.kb_chunks c JOIN mcp.kb_documents d ON d.id = c.document_id "
                "WHERE c.collection_id = ANY(:cids) "
                "AND c.text_tsv @@ websearch_to_tsquery('simple', :q)"
                f"{flt} ORDER BY score DESC, c.id LIMIT {CANDIDATES}"
            ),
            {"q": terms, "cids": collection_ids, **params},
        )
    ).all()
    return [_hit(r, float(r.score)) for r in rows]


async def vector_hits(
    session: Any,
    collection_ids: list[uuid.UUID],
    query_vec: list[float],
    dim: int,
    filters: dict[str, Any] | None,
) -> list[Hit]:
    if dim not in ALLOWED_DIMS or len(query_vec) != dim:
        raise ValidationFailed("Dimensi embedding query tidak cocok dengan koleksi")
    flt, params = _filter_sql(filters)
    base = {"cids": collection_ids, "dim": dim, **params}
    if await vector_mode(session) == "vector":
        await session.execute(text("SET LOCAL hnsw.ef_search = 64"))
        expr = f"(c.embedding::vector({dim})) <=> CAST(:qv AS real[])::vector({dim})"
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.document_id, c.chunk_index, c.text, d.title, "
                    f"d.source_type, d.source_ref, 1 - ({expr}) AS score "
                    "FROM mcp.kb_chunks c JOIN mcp.kb_documents d ON d.id = c.document_id "
                    f"WHERE c.embed_dim = :dim AND c.collection_id = ANY(:cids){flt} "
                    f"ORDER BY {expr} LIMIT {CANDIDATES}"
                ),
                {**base, "qv": [float(x) for x in query_vec]},
            )
        ).all()
        return [_hit(r, float(r.score)) for r in rows]
    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.embedding::real[] AS e FROM mcp.kb_chunks c "
                f"WHERE c.embed_dim = :dim AND c.collection_id = ANY(:cids){flt} "
                f"ORDER BY c.id DESC LIMIT {BRUTE_FORCE_MAX}"
            ),
            base,
        )
    ).all()
    if len(rows) == BRUTE_FORCE_MAX:
        log.warning("kb.brute_force_capped", limit=BRUTE_FORCE_MAX, hint="pasang pgvector")
    scores = cosine_scores(query_vec, [as_float_list(r.e) for r in rows])
    ranked = sorted(zip((r.id for r in rows), scores, strict=True), key=lambda x: -x[1])
    top = dict(ranked[:CANDIDATES])
    if not top:
        return []
    details = (
        await session.execute(text(f"{_SELECT}WHERE c.id = ANY(:ids)"), {"ids": list(top)})
    ).all()
    hits = [_hit(r, top[r.id]) for r in details]
    return sorted(hits, key=lambda h: -h.score)


def _hit(row: Any, score: float) -> Hit:
    return Hit(
        chunk_id=str(row.id),
        document_id=str(row.document_id),
        chunk_index=int(row.chunk_index),
        document_title=row.title,
        source=f"{row.source_type}:{row.source_ref}"[:300],
        text=row.text,
        score=score,
    )


def rrf(*lists: list[Hit], k: int = RRF_K) -> list[Hit]:
    """Reciprocal Rank Fusion: skor = Σ 1/(k + rank)."""
    fused: dict[str, float] = defaultdict(float)
    best: dict[str, Hit] = {}
    for hits in lists:
        for rank, hit in enumerate(hits, 1):
            fused[hit.chunk_id] += 1.0 / (k + rank)
            best.setdefault(hit.chunk_id, hit)
    out = [Hit(**{**best[cid].__dict__, "score": round(score, 6)}) for cid, score in fused.items()]
    return sorted(out, key=lambda h: -h.score)


def merge_adjacent(hits: list[Hit]) -> list[Hit]:
    """Chunk berurutan dari dokumen sama digabung (skor tertinggi dipakai)."""
    by_doc: dict[str, list[Hit]] = defaultdict(list)
    for hit in hits:
        by_doc[hit.document_id].append(hit)
    merged: list[Hit] = []
    for doc_hits in by_doc.values():
        doc_hits.sort(key=lambda h: h.chunk_index)
        group = [doc_hits[0]]
        for hit in doc_hits[1:]:
            if hit.chunk_index == group[-1].chunk_index + 1:
                group.append(hit)
                continue
            merged.append(_join(group))
            group = [hit]
        merged.append(_join(group))
    return sorted(merged, key=lambda h: -h.score)


def _join(group: list[Hit]) -> Hit:
    if len(group) == 1:
        return group[0]
    first = group[0]
    return Hit(
        chunk_id=first.chunk_id,
        document_id=first.document_id,
        chunk_index=first.chunk_index,
        document_title=first.document_title,
        source=first.source,
        text="\n".join(h.text for h in group),
        score=max(h.score for h in group),
    )


async def search(
    db: Any,
    collection_ids: list[uuid.UUID],
    query: str,
    *,
    mode: Mode,
    top_k: int,
    embedder: Embedder | None,
    dim: int,
    filters: dict[str, Any] | None = None,
    min_score: float | None = None,
) -> list[Hit]:
    query = query.strip()
    if not query:
        raise ValidationFailed("query kosong", details={"field": "query"})
    query_vec: list[float] | None = None
    if mode != "keyword":
        if embedder is None:
            raise ValidationFailed("Embedder tidak tersedia untuk mode vector/hybrid")
        query_vec = (await embedder.embed([query], "query"))[0]
    async with db.session() as session:
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        kw = await keyword_hits(session, collection_ids, query, filters) if mode != "vector" else []
        vec = (
            await vector_hits(session, collection_ids, query_vec, dim, filters)
            if query_vec is not None
            else []
        )
    if mode == "hybrid":
        hits = rrf(vec, kw)
    else:
        hits = vec or kw
    if min_score is not None:
        hits = [h for h in hits if h.score >= min_score]
    hits = merge_adjacent(hits)[:top_k]
    for hit in hits:
        hit.text = cut_text(hit.text)
    return hits
