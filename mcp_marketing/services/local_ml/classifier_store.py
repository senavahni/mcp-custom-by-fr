"""Simpan/muat head classifier lokal berversi (`mcp.local_classifiers`) + head sentimen.

`LocalSentimentHead` mengisi slot `method=model` TASK-20: embedding embed-local (interactive)
-> head softmax -> `(label, probs)`; `auto` tetap mengeskalasi ke LLM hanya bila confidence
rendah/sarkas (dan pemakai mengizinkan LLM).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.local_ml import LocalClassifier
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.local_ml.head import SoftmaxHead

log = get_logger(__name__)

SENTIMENT = "sentiment"
SENTIMENT_LABELS = ("positive", "neutral", "negative")
CACHE_TTL_S = 300.0


@dataclass(frozen=True)
class StoredHead:
    name: str
    version: int
    embed_model: str
    embed_dim: int
    head: SoftmaxHead
    metrics: dict[str, Any]

    @property
    def model_key(self) -> str:
        return f"head:{self.name}:v{self.version}:{self.embed_model}"


async def save_head(
    db: Any,
    name: str,
    head: SoftmaxHead,
    *,
    embed_model: str,
    metrics: dict[str, Any],
    dataset: dict[str, Any],
    activate: bool,
) -> int:
    """Simpan versi baru; `activate` mengarsipkan versi aktif sebelumnya. Kembalikan versi."""
    dim = int(head.weights.shape[0])
    async with db.session() as session:
        latest = await session.scalar(
            select(func.max(LocalClassifier.version)).where(LocalClassifier.name == name)
        )
        version = int(latest or 0) + 1
        if activate:
            await session.execute(
                update(LocalClassifier)
                .where(LocalClassifier.name == name, LocalClassifier.status == "active")
                .values(status="archived")
            )
        session.add(
            LocalClassifier(
                name=name,
                version=version,
                embed_model=embed_model,
                embed_dim=dim,
                labels=list(head.labels),
                params=head.to_json(),
                metrics=metrics,
                dataset=dataset,
                status="active" if activate else "draft",
                activated_at=func.now() if activate else None,
            )
        )
    return version


async def load_active(db: Any, name: str) -> StoredHead | None:
    async with db.session() as session:
        row = await session.scalar(
            select(LocalClassifier).where(
                LocalClassifier.name == name, LocalClassifier.status == "active"
            )
        )
    if row is None:
        return None
    return StoredHead(
        row.name,
        row.version,
        row.embed_model,
        row.embed_dim,
        SoftmaxHead.from_json(row.params),
        row.metrics,
    )


class LocalSentimentHead:
    """Predictor async untuk `SentimentAnalyzer` (format sama dengan ONNX `predict_batch`)."""

    def __init__(self, stored: StoredHead, embedder: Embedder) -> None:
        if embedder.model != stored.embed_model or embedder.dim != stored.embed_dim:
            raise ValidationFailed("Embedder tidak sama dengan model embedding head sentimen")
        self.stored = stored
        self.embedder = embedder
        self.model_key = stored.model_key

    async def predict(self, texts: list[str]) -> list[tuple[str, dict[str, float]]]:
        if not texts:
            return []
        return self.stored.head.predict(await self.embedder.embed(texts, "query"))


async def sentiment_head(services: Any) -> LocalSentimentHead | None:
    """Head sentimen aktif (cache 5 menit di `services.extras`); None bila belum dilatih."""
    extras = getattr(services, "extras", None)
    if extras is None or getattr(services, "db", None) is None:
        return None  # proses tanpa DB/extras (mis. test lama): perilaku ONNX/LLM lama
    injected = extras.get("sentiment_head")
    if injected is not None:
        return injected  # type: ignore[no-any-return]
    cached = extras.get("_sentiment_head_cache")
    if cached and time.monotonic() - cached[0] < CACHE_TTL_S:
        return cached[1]  # type: ignore[no-any-return]
    from mcp_marketing.services.kb.embedder import embedder_for

    try:
        stored = await load_active(services.db, SENTIMENT)
    except Exception as exc:  # noqa: BLE001 - tabel belum dimigrasi -> anggap belum ada head
        log.warning("local_ml.sentiment_head_unavailable", error=type(exc).__name__)
        stored = None
    head = (
        LocalSentimentHead(stored, embedder_for(services, stored.embed_model, stored.embed_dim))
        if stored is not None
        else None
    )
    extras["_sentiment_head_cache"] = (time.monotonic(), head)
    return head
