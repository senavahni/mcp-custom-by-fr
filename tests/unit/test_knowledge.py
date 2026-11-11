"""MCP-V1-11: upload streaming, ekstraksi PDF, chunk buku bersitasi, potongan kn_search."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcp_marketing.core.config import UploadSettings
from mcp_marketing.core.errors import Forbidden, ToolError, UnsupportedFile, UploadTooLarge
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.uploads import check_scope
from mcp_marketing.services.knowledge import chunking, pdf, uploads
from mcp_marketing.tools.kn import SNIPPET_MAX_CHARS, citation, snippet
from mcp_marketing.utils.tokens import count_tokens
from tests.pdf_factory import encrypted, make_pdf, with_outline

WS = uuid.UUID("00000000-0000-4000-8000-000000000001")
LOREM = (
    "Segmentasi pasar membagi pelanggan menjadi kelompok dengan kebutuhan yang mirip. "
    "Targeting memilih segmen yang paling menarik untuk dilayani. "
    "Positioning menempatkan merek di benak pelanggan sasaran."
)


async def _chunks(data: bytes, size: int = 1_000) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _settings(**over: int) -> UploadSettings:
    return UploadSettings(**over)


async def _store(
    data: bytes, tmp: Path, purpose: str = "book", **over: int
) -> uploads.StoredUpload:
    return await uploads.store_stream(
        _chunks(data),
        storage_dir=tmp,
        workspace_id=WS,
        purpose=purpose,  # type: ignore[arg-type]
        filename="../../etc/passwd.pdf",
        settings=_settings(**over),
    )


# ---------------------------------------------------------------------------- upload


async def test_pdf_upload_stored_by_sha_not_filename(tmp_path: Path) -> None:
    data = make_pdf([LOREM, LOREM])
    stored = await _store(data, tmp_path)
    assert stored.kind == "pdf" and stored.pages == 2 and stored.size_bytes == len(data)
    assert stored.path == tmp_path / "uploads" / str(WS) / f"{stored.sha256}.pdf"
    assert stored.path.read_bytes() == data
    assert not list((tmp_path / "uploads" / str(WS)).glob(".tmp-*"))
    again = await _store(data, tmp_path)  # dedupe: file sama -> path sama
    assert again.path == stored.path


async def test_upload_too_large_streaming_cleans_tmp(tmp_path: Path) -> None:
    data = make_pdf(["x" * 50] * 3) + b"0" * (1024 * 1024 + 10)
    with pytest.raises(UploadTooLarge):
        await _store(data, tmp_path, upload_pdf_max_mb=1)
    assert not list((tmp_path / "uploads" / str(WS)).iterdir())


async def test_pdf_page_limit(tmp_path: Path) -> None:
    with pytest.raises(UploadTooLarge):
        await _store(make_pdf([LOREM] * 3), tmp_path, pdf_max_pages=2)


@pytest.mark.parametrize(
    ("data", "purpose"),
    [
        (b"MZ\x90\x00binary", "book"),  # exe
        (b"PK\x03\x04word/document.xml", "import"),  # docx bukan xlsx
        (b"nama,alamat\nA,B\n", "book"),  # csv untuk buku
        (b"", "book"),
    ],
)
async def test_unsupported_files(tmp_path: Path, data: bytes, purpose: str) -> None:
    with pytest.raises(UnsupportedFile):
        await _store(data, tmp_path, purpose=purpose)


async def test_encrypted_and_corrupt_pdf_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFile, match="terenkripsi"):
        await _store(encrypted(make_pdf([LOREM])), tmp_path)
    with pytest.raises(UnsupportedFile):
        await _store(b"%PDF-1.4\nrusak" + b"x" * 100, tmp_path)


async def test_csv_and_cp1252_import(tmp_path: Path) -> None:
    stored = await _store(
        "nama,kota\nCafé Ária,Bandung\n".encode("cp1252"), tmp_path, purpose="import"
    )
    assert stored.kind == "csv" and stored.pages is None


def _client(**over: object) -> ClientIdentity:
    base: dict[str, object] = {
        "id": "c1",
        "name": "dashboard",
        "kind": "external",
        "toolsets": frozenset({"kn"}),
        "workspace_id": str(WS),
    }
    base.update(over)
    return ClientIdentity(**base)  # type: ignore[arg-type]


def test_upload_scope_rules() -> None:
    check_scope(_client(), WS, "book")
    with pytest.raises(Forbidden):
        check_scope(_client(), WS, "import")  # butuh ci
    with pytest.raises(Forbidden):
        check_scope(_client(), uuid.uuid4(), "book")  # workspace lain
    with pytest.raises(Forbidden):
        check_scope(_client(workspace_id=None), WS, "book")
    check_scope(_client(is_admin=True, toolsets=frozenset(), workspace_id=None), WS, "import")


# ---------------------------------------------------------------------------- PDF


def _write(tmp: Path, data: bytes) -> Path:
    path = tmp / "buku.pdf"
    path.write_bytes(data)
    return path


def test_extract_with_outline_and_scanned_page(tmp_path: Path) -> None:
    raw = make_pdf(["Pendahuluan\n" + LOREM, "", "Bab Dua\n" + LOREM, LOREM])
    data = with_outline(raw, [("Pendahuluan", 0, 0), ("Strategi STP", 2, 0), ("Segmentasi", 3, 1)])
    content = pdf.extract_pdf(_write(tmp_path, data), max_pages=10, timeout_s=60)
    assert content.structure == "outline"
    assert [p.needs_ocr for p in content.pages] == [False, True, False, False]
    assert [p.chapter for p in content.pages] == [
        "Pendahuluan",
        "Pendahuluan",
        "Strategi STP",
        "Strategi STP",
    ]
    assert content.pages[3].section == "Segmentasi"
    assert "Segmentasi pasar" in content.pages[0].text


def test_extract_heuristic_headings_without_outline(tmp_path: Path) -> None:
    data = make_pdf(["BAB 1 PENDAHULUAN\n" + LOREM, LOREM, "Chapter 2: Positioning\n" + LOREM])
    content = pdf.extract_pdf(_write(tmp_path, data), max_pages=10, timeout_s=60)
    assert content.structure == "heuristic"
    assert [p.chapter for p in content.pages] == [
        "BAB 1 PENDAHULUAN",
        "BAB 1 PENDAHULUAN",
        "Chapter 2: Positioning",
    ]


def test_extract_timeout(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="melebihi"):
        pdf.extract_pdf(_write(tmp_path, make_pdf([LOREM] * 3)), max_pages=10, timeout_s=-1)


# ---------------------------------------------------------------------------- chunk


def _pages() -> list[pdf.PageText]:
    long_para = " ".join([LOREM] * 12)  # ±400 token per halaman
    pages = [pdf.PageText(i, f"{long_para}\n\n{LOREM}", False, "Bab 1", None) for i in range(1, 6)]
    pages.append(pdf.PageText(6, "", True, "Bab 1", None))
    pages += [pdf.PageText(i, long_para, False, "Bab 2", "STP") for i in range(7, 9)]
    return pages


def test_chunks_respect_budget_pages_and_chapters() -> None:
    meta = {"asset_id": "a1", "kind": "book", "title": "Marketing", "edition": "3"}
    chunks = chunking.chunk_book(_pages(), title="Marketing", base_metadata=meta)
    assert chunks and all(c.tokens <= chunking.CHUNK_TOKENS for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.text.startswith(chunking.breadcrumb("Marketing", c.chapter, c.section))
        assert c.page_start <= c.page_end
        assert c.metadata["page_start"] == c.page_start and c.metadata["edition"] == "3"
        assert c.chapter in ("Bab 1", "Bab 2")
    assert all(6 not in (c.page_start, c.page_end) for c in chunks)  # halaman scan dilewati
    bab2 = [c for c in chunks if c.chapter == "Bab 2"]
    assert bab2 and all(c.page_start >= 7 for c in bab2)  # tidak menyeberang bab
    assert bab2[0].section == "STP" and "Marketing › Bab 2 › STP" in bab2[0].text


def test_overlap_repeats_tail_paragraph() -> None:
    chunks = chunking.chunk_book(_pages()[:5], title="M", base_metadata={})
    assert len(chunks) >= 2
    tail = chunks[0].text.rsplit("\n\n", 1)[-1]
    assert count_tokens(tail) <= chunking.OVERLAP_TOKENS
    assert tail in chunks[1].text


def test_giant_paragraph_split_by_sentence() -> None:
    page = pdf.PageText(1, " ".join([LOREM] * 60), False)
    chunks = chunking.chunk_book([page], title="M", base_metadata={})
    assert len(chunks) >= 3 and all(c.tokens <= chunking.CHUNK_TOKENS for c in chunks)


# ---------------------------------------------------------------------------- kn_search


def test_snippet_strips_breadcrumb_and_caps_length() -> None:
    text = "Marketing › Bab 1\n\n" + " ".join([LOREM] * 10)
    out = snippet(text)
    assert not out.startswith("Marketing ›")
    assert len(out) <= SNIPPET_MAX_CHARS and out.endswith("…")
    assert snippet("Judul\n\nPendek.") == "Pendek."


def test_citation_from_chunk_metadata() -> None:
    c = citation(
        {"asset_id": "a1", "title": "M", "chapter": "Bab 2", "page_start": 7, "page_end": 8}
    )
    assert (c.asset_id, c.chapter, c.page_start, c.page_end, c.section) == (
        "a1",
        "Bab 2",
        7,
        8,
        None,
    )


def test_settings_defaults_match_spec() -> None:
    s = UploadSettings()
    assert (s.upload_pdf_max_mb, s.upload_sheet_max_mb, s.pdf_max_pages) == (100, 20, 2_000)
    assert uploads.max_bytes("pdf", s) == 100 * 1024 * 1024
