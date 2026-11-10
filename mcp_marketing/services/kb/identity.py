"""Identitas model embedding yang dikunci per koleksi KB (TASK-19, MCP-V1-01).

- `embed-default` (Gateway remote, 1536) — koleksi lama tetap memakainya.
- `embed-local` (Gateway -> Qwen3-Embedding-0.6B OpenVINO GPU, 1024; V1-K7/K8) — koleksi v1
  `books:<ws>`, `companies:<ws>`, `social:<ws>` selalu memakai ini.
- `local/multilingual-e5-small` (ONNX CPU di proses MCP, 384) — mode dev lama.
"""

from __future__ import annotations

from typing import Literal

from mcp_marketing.core.config import Settings

GATEWAY_MODEL = "embed-default"
EMBED_LOCAL_MODEL = "embed-local"
EMBED_LOCAL_DIM = 1024
# chunk koleksi embed-local (spec MCP-V1-01); Gateway menolak > max_input_tokens (2.048)
EMBED_LOCAL_MAX_CHUNK_TOKENS = 1_024
LOCAL_MODEL = "local/multilingual-e5-small"
LOCAL_DIM = 384
V1_LOCAL_COLLECTION_PREFIXES = ("books:", "companies:", "social:")

EmbedModel = Literal["embed-default", "embed-local"]


def embed_identity(
    settings: Settings, *, collection_name: str | None = None, embed_model: str | None = None
) -> tuple[str, int]:
    """`(embed_model, embed_dim)` untuk koleksi baru.

    Urutan: koleksi v1 (prefiks) -> `embed_model` eksplisit -> `EMBED_PROVIDER`.
    """
    if collection_name and collection_name.startswith(V1_LOCAL_COLLECTION_PREFIXES):
        return EMBED_LOCAL_MODEL, EMBED_LOCAL_DIM
    if embed_model == EMBED_LOCAL_MODEL:
        return EMBED_LOCAL_MODEL, EMBED_LOCAL_DIM
    if embed_model == GATEWAY_MODEL:
        return GATEWAY_MODEL, settings.nlp.embed_dim
    if settings.nlp.embed_provider == "local":
        return LOCAL_MODEL, LOCAL_DIM
    return GATEWAY_MODEL, settings.nlp.embed_dim


def max_chunk_tokens(embed_model: str, requested: int) -> int:
    """Batas ukuran chunk per model koleksi (embed-local <= 1.024 token)."""
    if embed_model == EMBED_LOCAL_MODEL:
        return min(requested, EMBED_LOCAL_MAX_CHUNK_TOKENS)
    return requested
