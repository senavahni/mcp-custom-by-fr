"""Pembuat PDF kecil untuk test (teks per halaman, outline, enkripsi) tanpa dependensi tambahan."""

from __future__ import annotations

import io


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str]) -> bytes:
    """PDF valid: satu halaman per string; baris dipisah `\\n` (string kosong = halaman scan)."""
    objects: list[bytes] = []
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        lines = [ln for ln in text.split("\n")]
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for ln in lines:
            ops.append(f"({_escape(ln)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1") if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def with_outline(pdf: bytes, items: list[tuple[str, int, int]]) -> bytes:
    """Tambahkan bookmark `(judul, indeks_halaman_0, level)`; level 1 menempel ke level 0 terakhir."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf)))
    parent = None
    for title, page_index, level in items:
        node = writer.add_outline_item(title, page_index, parent=parent if level else None)
        if level == 0:
            parent = node
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def encrypted(pdf: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf)))
    writer.encrypt("rahasia")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
