"""Tipe kontak email lokal (V1-K6): `generic` / `role` / `personal` dari local-part.

Aturan: kata generik organisasi (info, admin, humas, ...) -> generic; jabatan/fungsi (hrd,
keuangan, kepsek, ...) -> role; pola nama orang (kamus nama Indonesia, `nama.nama`, inisial +
nama) -> personal. Ragu -> confidence rendah; pemakai mengirim ke review (kontak personal SELALU
direview manusia).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

ContactType = Literal["generic", "personal", "role"]
NAMES_FILE = Path(__file__).resolve().parent / "data" / "nama_id.txt"
REVIEW_BELOW = 0.6

GENERIC = frozenset(
    {
        "info", "informasi", "admin", "administrator", "contact", "kontak", "hubungi", "office",
        "kantor", "mail", "email", "halo", "hello", "hai", "cs", "customerservice", "customercare",
        "care", "support", "helpdesk", "layanan", "pelayanan", "pengaduan", "humas", "pr",
        "marketing", "sales", "penjualan", "order", "pemesanan", "reservasi", "booking",
        "sekretariat", "secretariat", "ppid", "redaksi", "noreply", "no-reply", "webmaster",
        "official", "resmi", "team", "tim", "general", "inquiry", "enquiry", "business", "bisnis",
    }
)  # fmt: skip
ROLE = frozenset(
    {
        "hrd", "hr", "sdm", "recruitment", "rekrutmen", "karir", "career", "careers", "finance",
        "keuangan", "accounting", "akuntansi", "purchasing", "pengadaan", "procurement",
        "direktur", "director", "direksi", "ceo", "owner", "kepsek", "kepalasekolah", "tu",
        "tatausaha", "akademik", "kesiswaan", "kurikulum", "bendahara", "rektor", "dekan",
        "legal", "it", "ti", "teknis", "operasional", "gudang", "logistik", "manager", "manajer",
        "admisi", "pmb", "ppdb", "humasrs", "igd", "rekammedis",
    }
)  # fmt: skip
_SPLIT = re.compile(r"[._\-+]+")


@dataclass(frozen=True)
class ContactTypeResult:
    contact_type: ContactType
    confidence: float
    reason: str

    @property
    def needs_review(self) -> bool:
        return self.contact_type == "personal" or self.confidence < REVIEW_BELOW


@lru_cache(maxsize=1)
def indonesian_names() -> frozenset[str]:
    lines = NAMES_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#"))


def classify_email(email: str) -> ContactTypeResult:
    local = email.split("@", 1)[0].lower()
    tokens = [t for t in _SPLIT.split(local) if t]
    stripped = [re.sub(r"\d+", "", t) for t in tokens]
    names = indonesian_names()
    if any(t in GENERIC for t in stripped) or local in GENERIC:
        return ContactTypeResult("generic", 0.9, "kata generik organisasi")
    if any(t in ROLE for t in stripped):
        return ContactTypeResult("role", 0.85, "jabatan/fungsi")
    name_hits = sum(1 for t in stripped if t in names)
    if name_hits >= 1 and len(stripped) <= 3:
        return ContactTypeResult("personal", 0.9 if name_hits >= 2 else 0.75, "pola nama orang")
    if len(stripped) == 2 and len(stripped[0]) == 1 and stripped[1].isalpha():
        return ContactTypeResult("personal", 0.7, "inisial + nama")
    if any(ch.isdigit() for ch in local) or len(local) > 20:
        return ContactTypeResult("generic", 0.6, "akun organisasi (angka/panjang)")
    # tidak dikenali: perlakukan sebagai kemungkinan personal (aman untuk UU PDP)
    return ContactTypeResult("personal", 0.4, "tidak dikenali")
