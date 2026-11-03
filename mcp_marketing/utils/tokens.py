"""Estimasi token (tiktoken `o200k_base`) untuk budget & pemangkasan output."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import orjson
import tiktoken

ENCODING = "o200k_base"
# di atas batas ini estimasi memakai rasio karakter (tokenisasi teks raksasa mahal)
EXACT_MAX_CHARS = 200_000
CHARS_PER_TOKEN = 4


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING)


def to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return orjson.dumps(value, default=str).decode()


def count_tokens(value: Any) -> int:
    text = to_text(value)
    if not text:
        return 0
    if len(text) > EXACT_MAX_CHARS:
        return len(text) // CHARS_PER_TOKEN
    return len(_encoding().encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    enc = _encoding()
    ids = enc.encode(text, disallowed_special=())
    if len(ids) <= max_tokens:
        return text
    return enc.decode(ids[: max(0, max_tokens - 1)]) + "…"
