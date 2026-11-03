"""Keyword-spotting aspek: hanya kalimat yang memuat sinonim aspek dikirim ke LLM (hemat token).

Kamus sinonim memakai bentuk BAKU (teks sudah dinormalisasi slang). Aspek di luar kamus memakai
nama aspek itu sendiri sebagai satu-satunya kata kunci.
"""

from __future__ import annotations

import re

ASPECT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "harga": ("harga", "mahal", "murah", "diskon", "promo", "ribu", "bayar", "potongan", "voucer"),
    "rasa": ("rasa", "enak", "manis", "pahit", "asin", "hambar", "gurih", "segar", "pedas", "basi"),
    "layanan": (
        "layanan",
        "pelayanan",
        "kasir",
        "barista",
        "ramah",
        "ketus",
        "admin",
        "penjual",
        "tanggapan",
        "antre",
    ),
    "pengiriman": ("kirim", "pengiriman", "kurir", "ongkos kirim", "terlambat", "sampai", "paket"),
    "kemasan": ("kemasan", "pengemasan", "packaging", "bocor", "penyok", "pecah", "botol", "segel"),
    "kualitas": ("kualitas", "asli", "palsu", "rusak", "awet", "bahan", "cacat"),
    "tempat": ("tempat", "gerai", "toko", "nyaman", "bersih", "kotor", "ramai", "parkir", "sepi"),
    "porsi": ("porsi", "besar", "kecil", "sedikit", "kenyang"),
}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\s*\n+\s*")


def synonyms(aspect: str) -> tuple[str, ...]:
    key = aspect.strip().lower()
    return ASPECT_SYNONYMS.get(key, (key,))


def _pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\w*", re.I)


def spot(text: str, aspects: list[str], max_chars: int = 280) -> list[tuple[str, str]]:
    """`[(aspek, kalimat)]` — satu kalimat per aspek per teks (kalimat pertama yang cocok)."""
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()] or [text]
    found: list[tuple[str, str]] = []
    for aspect in aspects:
        pattern = _pattern(synonyms(aspect))
        for sentence in sentences:
            if pattern.search(sentence):
                found.append((aspect.strip().lower(), sentence[:max_chars]))
                break
    return found
