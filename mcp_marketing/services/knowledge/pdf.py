"""Ekstraksi teks PDF per halaman (MCP-V1-11): pypdf, cadangan pdfplumber, outline -> bab.

- Halaman tanpa teks yang berarti (< 20 karakter) -> `needs_ocr=True` (scan; OCR = P2).
- Struktur: outline/bookmark PDF (level 0 = bab, level 1 = subbab); tanpa outline -> heuristik
  heading di awal halaman ("BAB 3 ...", "Chapter 2 ...", baris HURUF BESAR pendek).
- Batas waktu total ekstraksi; dijalankan di thread (CPU) oleh pemanggil.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mcp_marketing.core.errors import UnsupportedFile, UpstreamTimeout

MIN_TEXT_CHARS = 20
TITLE_MAX = 300
_HEADING = re.compile(r"^(bab|chapter|bagian|part)\s+([0-9]+|[ivxlc]+)\b[.:\s-]*(.*)$", re.I)
_CAPS_LINE = re.compile(r"^[A-Z0-9][A-Z0-9 ,:&'\-]{3,80}$")


@dataclass(frozen=True)
class PageText:
    page_no: int
    text: str
    needs_ocr: bool
    chapter: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class OutlineItem:
    title: str
    page_no: int
    level: int


@dataclass(frozen=True)
class PdfContent:
    pages: list[PageText]
    outline: list[OutlineItem]
    structure: str  # outline | heuristic | none


def _clean(text: str) -> str:
    lines = [" ".join(ln.split()) for ln in (text or "").splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _flatten_outline(reader: Any, items: list[Any], level: int, out: list[OutlineItem]) -> None:
    for item in items:
        if isinstance(item, list):
            _flatten_outline(reader, item, level + 1, out)
            continue
        try:
            page = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001 - tujuan bookmark rusak: lewati entri ini saja
            continue
        title = " ".join(str(getattr(item, "title", "") or "").split())[:TITLE_MAX]
        if title and page is not None and page >= 0:
            out.append(OutlineItem(title, page + 1, level))


def read_outline(reader: Any) -> list[OutlineItem]:
    out: list[OutlineItem] = []
    try:
        _flatten_outline(reader, list(reader.outline or []), 0, out)
    except Exception:  # noqa: BLE001 - outline opsional; ekstraksi teks tetap jalan
        return []
    return sorted(out, key=lambda o: (o.page_no, o.level))


def heading_of(text: str) -> str | None:
    """Heading bab di 3 baris pertama halaman (heuristik saat PDF tanpa outline)."""
    for line in text.splitlines()[:3]:
        line = line.strip()
        if not line:
            continue
        m = _HEADING.match(line)
        if m:
            return line[:TITLE_MAX]
        if _CAPS_LINE.match(line) and len(line.split()) <= 10 and not line.isdigit():
            return line.title()[:TITLE_MAX]
    return None


def assign_structure(
    pages: list[PageText], outline: list[OutlineItem]
) -> tuple[list[PageText], str]:
    """Isi chapter/section per halaman dari outline, atau heuristik heading."""
    if outline:
        min_level = min(o.level for o in outline)
        chapter = section = None
        by_page: dict[int, list[OutlineItem]] = {}
        for o in outline:
            by_page.setdefault(o.page_no, []).append(o)
        out = []
        for p in pages:
            for o in by_page.get(p.page_no, []):
                if o.level == min_level:
                    chapter, section = o.title, None
                elif o.level == min_level + 1:
                    section = o.title
            out.append(replace(p, chapter=chapter, section=section))
        return out, "outline"
    chapter = None
    found = False
    out = []
    for p in pages:
        heading = heading_of(p.text) if not p.needs_ocr else None
        if heading:
            chapter, found = heading, True
        out.append(replace(p, chapter=chapter))
    return out, "heuristic" if found else "none"


def _plumber_text(path: Path, index: int) -> str:
    import pdfplumber

    with pdfplumber.open(str(path), pages=[index + 1]) as pdf:
        if not pdf.pages:
            return ""
        return str(pdf.pages[0].extract_text() or "")


def extract_pdf(path: Path, *, max_pages: int, timeout_s: float) -> PdfContent:
    """Teks per halaman + outline. Sinkron (panggil lewat thread/process pool)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    started = time.monotonic()
    try:
        reader = PdfReader(str(path), strict=False)
    except PdfReadError:
        raise UnsupportedFile("PDF rusak atau tidak dapat dibaca") from None
    if reader.is_encrypted:
        raise UnsupportedFile("PDF terenkripsi/berpassword tidak didukung")
    total = len(reader.pages)
    if total > max_pages:
        raise UnsupportedFile(f"PDF {total} halaman melebihi batas {max_pages}")
    pages: list[PageText] = []
    for i, page in enumerate(reader.pages):
        if time.monotonic() - started > timeout_s:
            raise UpstreamTimeout(
                f"Ekstraksi PDF melebihi {int(timeout_s)} s (halaman {i + 1}/{total})",
                retryable=False,
            )
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - halaman bermasalah di pypdf -> coba pdfplumber
            raw = ""
        if len(raw.strip()) < MIN_TEXT_CHARS:
            try:
                raw = _plumber_text(path, i) or raw
            except Exception:  # noqa: BLE001 - tetap tandai needs_ocr
                pass
        text = _clean(raw)
        pages.append(PageText(i + 1, text, len(text) < MIN_TEXT_CHARS))
    outline = read_outline(reader)
    structured, kind = assign_structure(pages, outline)
    return PdfContent(structured, outline, kind)
