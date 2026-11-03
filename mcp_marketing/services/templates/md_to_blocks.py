"""Markdown ringkas -> blok BlockNote Dashboard (hemat token: LLM menulis markdown, bukan JSON).

Didukung: heading #..###, paragraf, bullet/numbered list, checklist `- [ ]`/`- [x]`, quote `>`,
code fence, tabel GFM, divider `---`, gambar `![alt](url)`, inline **bold** *italic* `code`
~~strike~~ [link](url). List bertingkat diratakan. Semua blok divalidasi `validate_block_content`.
"""

from __future__ import annotations

import re
from typing import Any

from mcp_marketing.schemas.blocks import (
    MAX_TABLE_COLS,
    MAX_TABLE_ROWS,
    BlockValidationError,
    extract_plain_text,
    is_safe_url,
    validate_block_content,
)

MAX_TEXT_PER_BLOCK = 9_000
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_CHECK = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*(\d{1,6})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9+#._-]*)\s*$")
_DIVIDER = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE = re.compile(
    r"\[(?P<ltext>[^\]]+)\]\((?P<href>[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|~~(?P<strike>.+?)~~"
    r"|`(?P<code>[^`]+)`"
    r"|(?<![\w*])\*(?P<italic>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<!\w)_(?P<italic2>[^_\s][^_]*?)_(?!\w)"
)


def _text(text: str, styles: dict[str, bool]) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": {k: v for k, v in styles.items() if v}}


def parse_inline(
    text: str, styles: dict[str, bool] | None = None, *, allow_links: bool = True
) -> list[dict[str, Any]]:
    base = dict(styles or {})
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(_text(text[pos : m.start()], base))
        if m.group("ltext") is not None:
            href = m.group("href")
            inner = parse_inline(m.group("ltext"), base, allow_links=False)
            if allow_links and is_safe_url(href, True):
                out.append(
                    {
                        "type": "link",
                        "href": href,
                        "content": [i for i in inner if i["type"] == "text"],
                    }
                )
            else:
                out.extend(inner)
        elif m.group("bold") is not None:
            out.extend(
                parse_inline(m.group("bold"), {**base, "bold": True}, allow_links=allow_links)
            )
        elif m.group("strike") is not None:
            out.extend(
                parse_inline(m.group("strike"), {**base, "strike": True}, allow_links=allow_links)
            )
        elif m.group("code") is not None:
            out.append(_text(m.group("code"), {**base, "code": True}))
        else:
            inner_text = m.group("italic") or m.group("italic2") or ""
            out.extend(parse_inline(inner_text, {**base, "italic": True}, allow_links=allow_links))
        pos = m.end()
    if pos < len(text):
        out.append(_text(text[pos:], base))
    return [i for i in out if i["type"] != "text" or i["text"]]


def _block(block_type: str, content: Any = None, **props: Any) -> dict[str, Any]:
    content_json: dict[str, Any] = {"props": props}
    if content is not None:
        content_json["content"] = content
    return {"block_type": block_type, "content_json": content_json}


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_TEXT_PER_BLOCK:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + len(sentence) + 1 > MAX_TEXT_PER_BLOCK:
            parts.append(current)
            current = ""
        while len(sentence) > MAX_TEXT_PER_BLOCK:
            parts.append(sentence[:MAX_TEXT_PER_BLOCK])
            sentence = sentence[MAX_TEXT_PER_BLOCK:]
        current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def _cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", stripped)]


def _table_blocks(header: list[str], rows: list[list[str]]) -> list[dict[str, Any]]:
    width = min(MAX_TABLE_COLS, max(len(header), *(len(r) for r in rows)) if rows else len(header))

    def row(cells: list[str]) -> dict[str, Any]:
        padded = (cells + [""] * width)[:width]
        return {"cells": [parse_inline(c) for c in padded]}

    blocks = []
    per_table = MAX_TABLE_ROWS - 1  # baris pertama = header
    for start in range(0, max(1, len(rows)), per_table):
        chunk = rows[start : start + per_table]
        content = {
            "type": "tableContent",
            "headerRows": 1,
            "rows": [row(header), *(row(r) for r in chunk)],
        }
        blocks.append(_block("table", content))
    return blocks


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(p.strip() for p in paragraph).strip()
            for part in _split_long(text):
                blocks.append(_block("paragraph", parse_inline(part)))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush_paragraph()
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            marker, language = fence.group(1), fence.group(2)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1
            props = {"language": language[:32]} if language else {}
            blocks.append(
                _block("codeBlock", [_text("\n".join(code)[:MAX_TEXT_PER_BLOCK], {})], **props)
            )
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_paragraph()
            header = _cells(line)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            blocks.extend(_table_blocks(header, rows))
            continue
        if _DIVIDER.match(line):
            flush_paragraph()
            blocks.append(_block("divider"))
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            level = min(3, len(heading.group(1)))
            blocks.append(_block("heading", parse_inline(heading.group(2).strip()), level=level))
            i += 1
            continue
        image = _IMAGE.match(line)
        if image and is_safe_url(image.group(2)):
            flush_paragraph()
            blocks.append(
                _block("image", None, url=image.group(2), caption=image.group(1)[:1000] or None)
            )
            i += 1
            continue
        check = _CHECK.match(line)
        if check:
            flush_paragraph()
            blocks.append(
                _block(
                    "checkListItem",
                    parse_inline(check.group(2).strip()),
                    checked=check.group(1).lower() == "x",
                )
            )
            i += 1
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush_paragraph()
            blocks.append(_block("bulletListItem", parse_inline(bullet.group(1).strip())))
            i += 1
            continue
        numbered = _NUMBERED.match(line)
        if numbered:
            flush_paragraph()
            blocks.append(_block("numberedListItem", parse_inline(numbered.group(2).strip())))
            i += 1
            continue
        quote = _QUOTE.match(line)
        if quote:
            flush_paragraph()
            quoted = [quote.group(1)]
            i += 1
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(_QUOTE.match(lines[i]).group(1))  # type: ignore[union-attr]
                i += 1
            blocks.append(
                _block("quote", parse_inline(" ".join(q.strip() for q in quoted).strip()))
            )
            continue
        paragraph.append(line)
        i += 1
    flush_paragraph()
    return [_validated(b) for b in blocks]


def _validated(block: dict[str, Any]) -> dict[str, Any]:
    props = {k: v for k, v in block["content_json"].get("props", {}).items() if v is not None}
    raw = {**block["content_json"], "props": props}
    try:
        cleaned = validate_block_content(block["block_type"], raw)
    except BlockValidationError:
        # fallback aman: tampilkan sebagai paragraf teks polos
        text = _plain(block)
        cleaned = validate_block_content(
            "paragraph", {"content": [_text(text[:MAX_TEXT_PER_BLOCK], {})]}
        )
        return {"block_type": "paragraph", "content_json": cleaned}
    return {"block_type": block["block_type"], "content_json": cleaned}


def _plain(block: dict[str, Any]) -> str:
    return extract_plain_text(block["block_type"], block["content_json"])
