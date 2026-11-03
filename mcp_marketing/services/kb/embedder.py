"""Embedder KB: Gateway (`embed-default`, 1536 dim) atau ONNX lokal (multilingual-e5-small, 384).

Model dikunci per koleksi (`kb_collections.embed_model/embed_dim`); embedder dipilih dari koleksi,
bukan dari setting saat ini, agar vektor dalam satu koleksi selalu sebanding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

from mcp_marketing.core.errors import UpstreamError, ValidationFailed
from mcp_marketing.services.kb.identity import GATEWAY_MODEL, LOCAL_DIM, LOCAL_MODEL

Kind = Literal["query", "passage"]
LOCAL_BATCH = 32
LOCAL_MAX_TOKENS = 512
LOCAL_REPO = "Xenova/multilingual-e5-small"
LOCAL_FILES = ("tokenizer.json", "onnx/model_quantized.onnx")


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


class GatewayEmbedder:
    """Lewat LLM Gateway; cache `embedding_cache` & pencatatan token ada di GatewayClient."""

    def __init__(self, llm: Any, dim: int, budget: Any = None) -> None:
        self.llm = llm
        self.model = GATEWAY_MODEL
        self.dim = dim
        self.budget = budget

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = await self.llm.embed(texts, budget=self.budget)
        if any(len(v) != self.dim for v in vectors):
            raise ValidationFailed(
                f"Dimensi embedding Gateway tidak sama dengan koleksi ({self.dim})",
                details={"got": len(vectors[0]) if vectors else 0},
            )
        return vectors


class LocalEmbedder:
    """ONNX CPU (tanpa PyTorch). Prefix e5: `query:` / `passage:`; mean pooling + normalisasi."""

    def __init__(self, model_dir: Path) -> None:
        self.model = LOCAL_MODEL
        self.dim = LOCAL_DIM
        self.model_dir = model_dir
        self._session: Any = None
        self._tokenizer: Any = None

    def _load(self) -> None:
        if self._session is not None:
            return
        onnx_path = self.model_dir / LOCAL_FILES[1]
        tok_path = self.model_dir / LOCAL_FILES[0]
        if not onnx_path.exists() or not tok_path.exists():
            raise UpstreamError(
                f"Model embedding lokal belum diunduh ke {self.model_dir}; "
                "jalankan `python scripts/manage.py download-models`",
                retryable=False,
            )
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(tok_path))
        tokenizer.enable_truncation(max_length=LOCAL_MAX_TOKENS)
        tokenizer.enable_padding()
        self._tokenizer = tokenizer
        self._session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        self._load()
        out: list[list[float]] = []
        input_names = {i.name for i in self._session.get_inputs()}
        for start in range(0, len(texts), LOCAL_BATCH):
            encoded = self._tokenizer.encode_batch(texts[start : start + LOCAL_BATCH])
            ids = np.asarray([e.ids for e in encoded], dtype=np.int64)
            mask = np.asarray([e.attention_mask for e in encoded], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feeds)[0]
            out.extend(mean_pool_normalize(hidden, mask))
        return out

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        if not texts:
            return []
        prefixed = [f"{kind}: {t}" for t in texts]
        return await asyncio.to_thread(self._embed_sync, prefixed)


def mean_pool_normalize(hidden: Any, mask: Any) -> list[list[float]]:
    weights = mask[..., None].astype(np.float32)
    summed = (hidden * weights).sum(axis=1)
    counts = np.clip(weights.sum(axis=1), 1e-9, None)
    pooled = summed / counts
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return [[float(x) for x in row] for row in pooled / norms]


def embedder_for(services: Any, embed_model: str, embed_dim: int, budget: Any = None) -> Embedder:
    """Embedder sesuai model yang dikunci koleksi (bisa disuntik lewat `services.extras`)."""
    injected = services.extras.get("kb_embedder")
    if injected is not None:
        return injected  # type: ignore[no-any-return]
    if embed_model == LOCAL_MODEL:
        cache = services.extras.setdefault("kb_local_embedder", {})
        model_dir = services.settings.nlp.local_embed_model_dir
        key = str(model_dir)
        if key not in cache:
            cache[key] = LocalEmbedder(model_dir)
        return cache[key]  # type: ignore[no-any-return]
    if embed_model == GATEWAY_MODEL:
        if services.llm is None:
            raise UpstreamError("LLM Gateway tidak dikonfigurasi untuk embedding", retryable=False)
        return GatewayEmbedder(services.llm, embed_dim, budget)
    raise ValidationFailed(f"Model embedding koleksi tidak dikenal: {embed_model}")
