"""Identitas model embedding yang dikunci per koleksi KB (TASK-19)."""

from __future__ import annotations

from mcp_marketing.core.config import Settings

GATEWAY_MODEL = "embed-default"
LOCAL_MODEL = "local/multilingual-e5-small"
LOCAL_DIM = 384


def embed_identity(settings: Settings) -> tuple[str, int]:
    """`(embed_model, embed_dim)` untuk koleksi baru sesuai `EMBED_PROVIDER`."""
    if settings.nlp.embed_provider == "local":
        return LOCAL_MODEL, LOCAL_DIM
    return GATEWAY_MODEL, settings.nlp.embed_dim
