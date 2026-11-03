"""Chunker markdown-aware untuk KB (TASK-19).

Urutan pemecahan: heading -> blok (paragraf/list/tabel/kode) -> kalimat. Tiap chunk diawali
breadcrumb heading (`[Brief > Target Audiens] …`) agar mandiri saat diambil sendirian; tabel hanya
dipotong di batas baris (header diulang); chunk < 30 token digabung dengan tetangganya.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mcp_marketing.utils.tokens import count_tokens

DEFAULT_CHUNK_TOKENS = 800
DEFAULT_OVERLAP = 100
MIN_CHUNK_TOKENS = 30
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=\S)")


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int
    breadcrumb: str


@dataclass
class _Section:
    path: list[str]
    blocks: list[str]


def _sections(markdown: str, title: str | None) -> list[_Section]:
    stack: list[tuple[int, str]] = []
    sections = [_Section([title] if title else [], [])]
    buf: list[str] = []
    in_code = False

    def flush() -> None:
        if buf:
            sections[-1].blocks.append("\n".join(buf).strip())
            buf.clear()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_code = not in_code
            buf.append(line)
            if not in_code:
                flush()
            continue
        if in_code:
            buf.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, heading.group(2))]
            sections.append(_Section(([title] if title else []) + [t for _, t in stack], []))
            continue
        is_table = line.lstrip().startswith("|")
        prev_table = bool(buf) and buf[-1].lstrip().startswith("|")
        if not line.strip() or (buf and is_table != prev_table):
            flush()
        if line.strip():
            buf.append(line)
    flush()
    return [s for s in sections if s.blocks]


def _split_table(block: str, budget: int) -> list[str]:
    rows = block.splitlines()
    separator = len(rows) > 1 and set(rows[1].replace("|", "").strip()) <= set("-: ")
    header = rows[:2] if separator else rows[:1]
    body = rows[len(header) :]
    parts: list[str] = []
    current: list[str] = []
    for row in body:
        candidate = "\n".join([*header, *current, row])
        if current and count_tokens(candidate) > budget:
            parts.append("\n".join([*header, *current]))
            current = []
        current.append(row)
    if current or not parts:
        parts.append("\n".join([*header, *current]))
    return parts


def _units(block: str, budget: int) -> list[str]:
    """Pecah blok terlalu besar: tabel per baris, teks per kalimat, kalimat raksasa per kata."""
    if count_tokens(block) <= budget:
        return [block]
    if block.lstrip().startswith("|"):
        return _split_table(block, budget)
    out: list[str] = []
    for sentence in _SENTENCE.split(block):
        if count_tokens(sentence) <= budget:
            out.append(sentence)
            continue
        words, cur = sentence.split(), list[str]()
        for word in words:
            if cur and count_tokens(" ".join([*cur, word])) > budget:
                out.append(" ".join(cur))
                cur = []
            cur.append(word)
        if cur:
            out.append(" ".join(cur))
    return out


def _tail(units: list[str], overlap: int) -> list[str]:
    tail: list[str] = []
    for unit in reversed(units):
        if count_tokens("\n\n".join([unit, *tail])) > overlap:
            break
        tail.insert(0, unit)
    return tail


def _pack(units: list[str], budget: int, overlap: int) -> list[str]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for unit in units:
        if current and count_tokens("\n\n".join([*current, unit])) > budget:
            chunks.append(current)
            current = [u for u in _tail(current, overlap) if u != unit]
            if current and count_tokens("\n\n".join([*current, unit])) > budget:
                current = []
        current.append(unit)
    if current:
        chunks.append(current)
    return ["\n\n".join(c) for c in chunks]


def chunk_markdown(
    markdown: str,
    *,
    title: str | None = None,
    chunk_size: int = DEFAULT_CHUNK_TOKENS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    if chunk_size < 100 or not 0 <= overlap < chunk_size // 2:
        raise ValueError("chunk_size >= 100 dan 0 <= overlap < chunk_size/2")
    pieces: list[tuple[str, str]] = []  # (breadcrumb, body)
    for section in _sections(markdown, title):
        crumb = " > ".join(p for p in section.path if p)
        prefix_tokens = count_tokens(f"[{crumb}] ") if crumb else 0
        budget = max(50, chunk_size - prefix_tokens)
        units = [u for block in section.blocks for u in _units(block, budget)]
        pieces.extend((crumb, body) for body in _pack(units, budget, overlap))
    rendered = [(crumb, f"[{crumb}] {body}" if crumb else body) for crumb, body in pieces]
    merged = _merge_small(rendered, chunk_size)
    return [
        Chunk(index=i, text=text, tokens=count_tokens(text), breadcrumb=crumb)
        for i, (crumb, text) in enumerate(merged)
    ]


def _merge_small(pieces: list[tuple[str, str]], chunk_size: int) -> list[tuple[str, str]]:
    """Gabungkan potongan < MIN_CHUNK_TOKENS ke sebelumnya (bila muat) atau ke berikutnya."""
    merged: list[tuple[str, str]] = []
    pending: tuple[str, str] | None = None
    for crumb, text in pieces:
        if pending is not None:
            joined = f"{pending[1]}\n\n{text}"
            if count_tokens(joined) <= chunk_size:
                crumb, text = pending[0], joined
            else:
                merged.append(pending)
            pending = None
        if count_tokens(text) < MIN_CHUNK_TOKENS:
            if merged and count_tokens(f"{merged[-1][1]}\n\n{text}") <= chunk_size:
                merged[-1] = (merged[-1][0], f"{merged[-1][1]}\n\n{text}")
            else:
                pending = (crumb, text)
            continue
        merged.append((crumb, text))
    if pending is not None:
        merged.append(pending)
    return merged
