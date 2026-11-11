"""Tugas teks lokal berbasis embedding: aspek, kebutuhan ekstraktif, pemecah kalimat.

- Aspek: kalimat <-> deskripsi `product_features` + aspek umum (harga, layanan, kualitas, ...).
- Kebutuhan ekstraktif: kalimat situs paling mirip prototipe "kebutuhan/masalah/rencana" ->
  KUTIPAN asli (<= 300 karakter), bukan ringkasan.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.local_ml.vectors import as_matrix, cosine

ASPECT_THRESHOLD = 0.45
NEED_THRESHOLD = 0.50
QUOTE_MAX_CHARS = 300
MIN_SENTENCE_CHARS = 25
GENERIC_ASPECTS: dict[str, str] = {
    "harga": "harga, biaya, mahal, murah, tarif, diskon, promo",
    "layanan": "pelayanan, customer service, respon, admin, bantuan, komplain",
    "kualitas": "kualitas produk, awet, rusak, bagus, cacat, mutu",
    "pengiriman": "pengiriman, ongkir, kurir, paket terlambat, sampai",
    "kemudahan": "mudah dipakai, ribet, aplikasi, fitur, cara pakai",
}
NEED_PROTOTYPES: dict[str, list[str]] = {
    "need": [
        "kami membutuhkan sistem untuk mengelola data",
        "sekolah kami memerlukan aplikasi untuk administrasi",
        "kami sedang mencari solusi untuk meningkatkan layanan",
    ],
    "problem": [
        "kendala utama kami adalah proses yang masih manual",
        "tantangan kami saat ini adalah data yang tersebar",
        "masalah yang sering terjadi adalah antrean panjang",
    ],
    "plan": [
        "tahun depan kami berencana melakukan digitalisasi layanan",
        "kami akan mengembangkan sistem informasi terpadu",
        "program kerja kami meliputi peningkatan infrastruktur teknologi",
    ],
}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class NeedQuote:
    quote: str
    category: str
    score: float


def split_sentences(text: str) -> list[str]:
    parts = (" ".join(p.split()) for p in _SENT_SPLIT.split(text or ""))
    return [p for p in parts if len(p) >= MIN_SENTENCE_CHARS]


async def match_aspects(
    sentences: list[str],
    aspects: dict[str, str],
    embedder: Embedder,
    *,
    threshold: float = ASPECT_THRESHOLD,
) -> list[list[tuple[str, float]]]:
    """Per kalimat: daftar (aspek, skor) >= ambang, urut skor menurun."""
    if not sentences or not aspects:
        return [[] for _ in sentences]
    names = list(aspects)
    s = as_matrix(await embedder.embed(sentences, "query"))
    a = as_matrix(await embedder.embed([f"{n}: {d}" for n, d in aspects.items()], "passage"))
    scores = cosine(s, a)
    out = []
    for row in scores:
        hits = [
            (names[j], round(float(row[j]), 4)) for j in np.argsort(-row) if row[j] >= threshold
        ]
        out.append(hits)
    return out


async def extract_needs(
    text: str,
    embedder: Embedder,
    *,
    threshold: float = NEED_THRESHOLD,
    max_quotes: int = 5,
) -> list[NeedQuote]:
    """Kutipan kalimat yang paling mirip prototipe kebutuhan/masalah/rencana (ekstraktif)."""
    sentences = split_sentences(text)
    if not sentences:
        return []
    protos = [(cat, p) for cat, items in NEED_PROTOTYPES.items() for p in items]
    s = as_matrix(await embedder.embed(sentences, "passage"))
    p = as_matrix(await embedder.embed([t for _, t in protos], "query"))
    scores = cosine(s, p)
    found: list[NeedQuote] = []
    for i, row in enumerate(scores):
        j = int(np.argmax(row))
        if row[j] >= threshold:
            found.append(
                NeedQuote(sentences[i][:QUOTE_MAX_CHARS], protos[j][0], round(float(row[j]), 4))
            )
    found.sort(key=lambda q: -q.score)
    return _dedupe(found)[:max_quotes]


def _dedupe(quotes: Sequence[NeedQuote]) -> list[NeedQuote]:
    seen: set[str] = set()
    out = []
    for q in quotes:
        key = q.quote.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out
