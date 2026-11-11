"""Penyimpanan upload streaming (MCP-V1-11): batas ukuran, magic bytes, dedupe sha256.

File ditulis ke `STORAGE_DIR/uploads/<workspace_id>/<sha256>.<ext>`. Nama file asli TIDAK pernah
dipakai sebagai path (hanya metadata) sehingga path traversal tidak mungkin; `workspace_id` UUID
tervalidasi, sha256 hex, ekstensi dari daftar tetap. Ukuran dicek selama streaming (bukan setelah
selesai) sehingga file 101 MB tidak pernah ditulis penuh.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcp_marketing.core.config import UploadSettings
from mcp_marketing.core.errors import UnsupportedFile, UploadTooLarge, ValidationFailed

Kind = Literal["pdf", "xlsx", "csv"]
Purpose = Literal["book", "brand", "article", "import"]
SNIFF_BYTES = 8_192
MB = 1024 * 1024
PURPOSE_KINDS: dict[str, tuple[str, ...]] = {
    "book": ("pdf",),
    "brand": ("pdf",),
    "article": ("pdf",),
    "import": ("xlsx", "csv"),
}


@dataclass(frozen=True)
class StoredUpload:
    sha256: str
    kind: Kind
    size_bytes: int
    path: Path
    pages: int | None


def sniff_kind(head: bytes, filename: str | None) -> Kind:
    """Jenis file dari magic bytes (nama/Content-Type tidak dipercaya)."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        # zip: hanya XLSX (ada [Content_Types].xml + xl/), divalidasi lagi saat import
        if (
            b"[Content_Types].xml" in head
            or b"xl/" in head
            or (filename or "").lower().endswith(".xlsx")
        ):
            return "xlsx"
        raise UnsupportedFile("Arsip zip bukan XLSX")
    if b"\x00" in head:
        raise UnsupportedFile("File biner tidak didukung (hanya PDF, XLSX, CSV)")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("cp1252")  # CSV Windows-1252 (MCP-V1-08)
        except UnicodeDecodeError:
            raise UnsupportedFile("Teks bukan CSV yang dapat dibaca") from None
    return "csv"


def max_bytes(kind: str, settings: UploadSettings) -> int:
    mb = settings.upload_pdf_max_mb if kind == "pdf" else settings.upload_sheet_max_mb
    return mb * MB


def check_pdf(path: Path, max_pages: int) -> int:
    """PDF valid, tidak terenkripsi, <= `max_pages` halaman. Kembalikan jumlah halaman."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise UnsupportedFile("PDF terenkripsi/berpassword tidak didukung")
        pages = len(reader.pages)
    except PdfReadError:
        raise UnsupportedFile("PDF rusak atau tidak dapat dibaca") from None
    if pages < 1:
        raise UnsupportedFile("PDF tanpa halaman")
    if pages > max_pages:
        raise UploadTooLarge(
            f"PDF {pages} halaman melebihi batas {max_pages} halaman",
            details={"pages": pages, "max_pages": max_pages},
        )
    return pages


def workspace_dir(storage_dir: Path, workspace_id: uuid.UUID) -> Path:
    return storage_dir / "uploads" / str(uuid.UUID(str(workspace_id)))


async def store_stream(
    chunks: AsyncIterator[bytes],
    *,
    storage_dir: Path,
    workspace_id: uuid.UUID,
    purpose: Purpose,
    filename: str | None,
    settings: UploadSettings,
) -> StoredUpload:
    if purpose not in PURPOSE_KINDS:
        raise ValidationFailed(f"purpose tidak dikenal: {purpose}")
    folder = workspace_dir(storage_dir, workspace_id)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".tmp-{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    head = b""
    kind: Kind | None = None
    limit = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in chunks:
                if not chunk:
                    continue
                if kind is None:
                    head += chunk
                    if len(head) < SNIFF_BYTES:
                        continue
                    kind, limit = _decide(head, filename, purpose, settings)
                    chunk, head = head, b""
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(
                        f"File melebihi batas {limit // MB} MB", details={"max_mb": limit // MB}
                    )
                digest.update(chunk)
                out.write(chunk)
            if kind is None:  # file kecil (< SNIFF_BYTES)
                if not head:
                    raise UnsupportedFile("File kosong")
                kind, limit = _decide(head, filename, purpose, settings)
                size = len(head)
                digest.update(head)
                out.write(head)
        pages = (
            await asyncio.to_thread(check_pdf, tmp, settings.pdf_max_pages)
            if kind == "pdf"
            else None
        )
        sha = digest.hexdigest()
        final = folder / f"{sha}.{kind}"
        if final.exists():
            tmp.unlink()
        else:
            os.replace(tmp, final)
        return StoredUpload(sha, kind, size, final, pages)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _decide(
    head: bytes, filename: str | None, purpose: str, settings: UploadSettings
) -> tuple[Kind, int]:
    kind = sniff_kind(head, filename)
    if kind not in PURPOSE_KINDS[purpose]:
        raise UnsupportedFile(
            f"File {kind} tidak cocok untuk purpose {purpose}",
            details={"kind": kind, "allowed": list(PURPOSE_KINDS[purpose])},
        )
    return kind, max_bytes(kind, settings)
