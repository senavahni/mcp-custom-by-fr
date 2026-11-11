"""Chunk semantik buku (MCP-V1-11): <= 800 token, overlap ±100, breadcrumb, rentang halaman.

Paragraf dikumpulkan per (bab, subbab) — chunk tidak menyeberangi batas bab/subbab — lalu
dipadatkan sampai batas token. Overlap = paragraf ekor chunk sebelumnya (<= 100 token).
Paragraf yang terlalu panjang dipecah per kalimat. Setiap chunk membawa breadcrumb
"Judul › Bab › Subbab" + metadata sitasi `{asset_id, kind, title, edition, chapter, section,
page_start, page_end}`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import groupby
from typing import Any

from mcp_marketing.services.knowledge.pdf import PageText
from mcp_marketing.utils.tokens import count_tokens

CHUNK_TOKENS = 800
OVERLAP_TOKENS = 100
_PARA = re.compile(r"\n\s*\n|\n(?=[A-Z0-9•\-–])")
_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Para:
    text: str
    page_no: int
    tokens: int


@dataclass
class BookChunk:
    index: int
    text: str
    tokens: int
    page_start: int
    page_end: int
    chapter: str | None
    section: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


def breadcrumb(title: str, chapter: str | None, section: str | None) -> str:
    return " › ".join(p for p in (title, chapter, section) if p)


def paragraphs(page: PageText, max_tokens: int) -> list[Para]:
    out: list[Para] = []
    for raw in _PARA.split(page.text):
        text = " ".join(raw.split())
        if not text:
            continue
        tokens = count_tokens(text)
        if tokens <= max_tokens:
            out.append(Para(text, page.page_no, tokens))
            continue
        buf: list[str] = []
        for sent in _SENT.split(text):  # paragraf raksasa -> potong per kalimat
            if buf and count_tokens(" ".join([*buf, sent])) > max_tokens:
                joined = " ".join(buf)
                out.append(Para(joined, page.page_no, count_tokens(joined)))
                buf = []
            buf.append(sent)
        if buf:
            joined = " ".join(buf)
            out.append(Para(joined, page.page_no, count_tokens(joined)))
    return out


def _pack(paras: list[Para], budget: int, overlap: int) -> list[list[Para]]:
    groups: list[list[Para]] = []
    current: list[Para] = []
    used = 0
    for p in paras:
        if current and used + p.tokens > budget:
            groups.append(current)
            tail: list[Para] = []
            tail_tokens = 0
            for q in reversed(current):  # ekor <= overlap token sebagai konteks
                if tail_tokens + q.tokens > overlap:
                    break
                tail.insert(0, q)
                tail_tokens += q.tokens
            current, used = tail, tail_tokens
        current.append(p)
        used += p.tokens
    if current:
        groups.append(current)
    return groups


def chunk_book(
    pages: list[PageText],
    *,
    title: str,
    base_metadata: dict[str, Any],
    chunk_tokens: int = CHUNK_TOKENS,
    overlap: int = OVERLAP_TOKENS,
) -> list[BookChunk]:
    usable = [p for p in pages if not p.needs_ocr and p.text]
    chunks: list[BookChunk] = []
    for (chapter, section), group in groupby(usable, key=lambda p: (p.chapter, p.section)):
        crumb = breadcrumb(title, chapter, section)
        budget = max(100, chunk_tokens - count_tokens(crumb) - 2)
        paras = [para for page in group for para in paragraphs(page, budget)]
        for packed in _pack(paras, budget, overlap):
            body = "\n\n".join(p.text for p in packed)
            text = f"{crumb}\n\n{body}"
            meta = {
                **base_metadata,
                "chapter": chapter,
                "section": section,
                "page_start": packed[0].page_no,
                "page_end": packed[-1].page_no,
            }
            chunks.append(
                BookChunk(
                    index=len(chunks),
                    text=text,
                    tokens=count_tokens(text),
                    page_start=packed[0].page_no,
                    page_end=packed[-1].page_no,
                    chapter=chapter,
                    section=section,
                    metadata=meta,
                )
            )
    return chunks
