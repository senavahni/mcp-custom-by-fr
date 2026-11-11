"""Klasifikasi KBLI 2020 + ISIC Rev.4 lokal: embedding teks perusahaan -> kNN `industries`.

- Sisi dokumen: `industry_text` (kode, judul, uraian) di-embed sekali (`embed_industries`,
  `manage.py industries-embed`) ke `mcp.industries.embedding` (embed-local, 1024).
- Sisi query: nama + sektor + deskripsi perusahaan (`company_text`), `input_type=query`.
- Top-3 + skor; otomatis bila skor top-1 >= 0,55 DAN selisih top-1/top-2 >= 0,05, selain itu
  `review` (atau LLM assist bila diaktifkan pemakai).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from sqlalchemy import text

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.kb.identity import EMBED_LOCAL_MODEL
from mcp_marketing.services.local_ml.vectors import Matrix, as_matrix, cosine, top_k

AUTO_MIN_SCORE = 0.55
AUTO_MIN_MARGIN = 0.05
TOP_N = 3
DESCRIPTION_CHARS = 600
EMBED_BATCH = 64
DEFAULT_LEVELS: tuple[str, ...] = ("subclass",)
Decision = Literal["auto", "review"]


@dataclass(frozen=True)
class KbliCandidate:
    kbli_code: str
    name: str
    score: float
    isic_code: str | None


@dataclass(frozen=True)
class KbliResult:
    candidates: list[KbliCandidate]
    decision: Decision

    @property
    def best(self) -> KbliCandidate | None:
        return self.candidates[0] if self.candidates else None


def industry_text(code: str, name_id: str, description: str | None) -> str:
    desc = " ".join((description or "").split())[:DESCRIPTION_CHARS]
    return f"KBLI {code}: {name_id.capitalize()}. {desc}".strip()


def company_text(name: str, sector: str | None = None, description: str | None = None) -> str:
    parts = [name.strip()]
    if sector:
        parts.append(f"Sektor: {sector.strip()}")
    if description:
        parts.append(" ".join(description.split())[:DESCRIPTION_CHARS])
    return ". ".join(p for p in parts if p)


def decide(scores: Sequence[float]) -> Decision:
    if not scores:
        return "review"
    margin = scores[0] - scores[1] if len(scores) > 1 else scores[0]
    return "auto" if scores[0] >= AUTO_MIN_SCORE and margin >= AUTO_MIN_MARGIN else "review"


class KbliIndex:
    """Indeks in-memory (±1.800 baris x 1024 ≈ 7 MB) — kNN numpy cepat tanpa pgvector."""

    def __init__(
        self,
        codes: list[str],
        names: list[str],
        isic: list[str | None],
        vectors: Sequence[Sequence[float]] | Matrix,
    ) -> None:
        if not codes:
            raise ValidationFailed(
                "Embedding KBLI belum dibuat; jalankan `python scripts/manage.py industries-embed`"
            )
        self.codes, self.names, self.isic = codes, names, isic
        self.matrix = as_matrix(vectors)

    @classmethod
    async def load(cls, db: Any, *, levels: tuple[str, ...] = DEFAULT_LEVELS) -> KbliIndex:
        async with db.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT i.kbli_code, i.name_id, i.embedding::real[] AS e, "
                        "(SELECT m.isic_code FROM mcp.industry_isic_map m "
                        " WHERE m.kbli_code = i.kbli_code ORDER BY length(m.isic_code) DESC "
                        " LIMIT 1) AS isic "
                        "FROM mcp.industries i WHERE i.embedding IS NOT NULL "
                        "AND i.embed_model = :m AND i.level = ANY(:lv) ORDER BY i.kbli_code"
                    ),
                    {"m": EMBED_LOCAL_MODEL, "lv": list(levels)},
                )
            ).all()
        return cls(
            [r.kbli_code for r in rows],
            [r.name_id for r in rows],
            [r.isic for r in rows],
            [list(r.e) for r in rows],
        )

    def classify_vectors(self, vectors: Sequence[Sequence[float]] | Matrix) -> list[KbliResult]:
        scores = cosine(as_matrix(vectors), self.matrix)
        out: list[KbliResult] = []
        for row in scores:
            idx = top_k(row, TOP_N)
            cands = [
                KbliCandidate(self.codes[i], self.names[i], round(float(row[i]), 4), self.isic[i])
                for i in idx
            ]
            out.append(KbliResult(cands, decide([c.score for c in cands])))
        return out

    async def classify(self, texts: list[str], embedder: Embedder) -> list[KbliResult]:
        if not texts:
            return []
        return self.classify_vectors(await embedder.embed(texts, "query"))


async def embed_industries(
    db: Any, embedder: Embedder, *, levels: tuple[str, ...] = DEFAULT_LEVELS, force: bool = False
) -> int:
    """Isi `industries.embedding` (embed-local) untuk baris yang belum/berbeda model. Idempoten."""
    if embedder.model != EMBED_LOCAL_MODEL:
        raise ValidationFailed("Embedding KBLI wajib memakai embed-local (1024 dim)")
    cond = "" if force else "AND (embedding IS NULL OR embed_model IS DISTINCT FROM :m)"
    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT kbli_code, name_id, description FROM mcp.industries "
                    f"WHERE level = ANY(:lv) {cond} ORDER BY kbli_code"
                ),
                {"lv": list(levels), "m": EMBED_LOCAL_MODEL},
            )
        ).all()
    done = 0
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start : start + EMBED_BATCH]
        vectors = await embedder.embed(
            [industry_text(r.kbli_code, r.name_id, r.description) for r in batch], "passage"
        )
        async with db.session() as session:
            for r, vec in zip(batch, vectors, strict=True):
                await session.execute(
                    text(
                        "UPDATE mcp.industries SET embedding = CAST(:e AS real[]), "
                        "embed_model = :m, updated_at = now() WHERE kbli_code = :c"
                    ),
                    {
                        "e": [float(x) for x in np.asarray(vec)],
                        "m": EMBED_LOCAL_MODEL,
                        "c": r.kbli_code,
                    },
                )
        done += len(batch)
    return done
