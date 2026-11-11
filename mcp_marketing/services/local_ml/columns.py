"""Mapping kolom file import -> field target, lokal (MCP-V1-04, dipakai MCP-V1-08).

Skor gabungan per pasangan (kolom, field) = 0,55 x pola nilai + 0,45 x kemiripan embedding
(header + sampel <-> deskripsi field). Penugasan satu-ke-satu terbaik (Hungarian,
`scipy.optimize.linear_sum_assignment`); pasangan < ambang tidak dipetakan -> `extra` (tidak
dibuang). Pola nilai saja sudah cukup untuk kolom berpola jelas (email, telepon, kode pos, URL).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field

import numpy as np
from scipy.optimize import linear_sum_assignment

from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.local_ml.vectors import as_matrix, cosine

W_PATTERN = 0.55
W_SEMANTIC = 0.45
MIN_CONFIDENCE = 0.40
SAMPLE_VALUES = 5
SAMPLE_CHARS = 60

# field target import (MCP-V1-08) -> deskripsi untuk kemiripan embedding
FIELD_DESCRIPTIONS: dict[str, str] = {
    "name": "nama perusahaan / instansi / sekolah / rumah sakit / organisasi (company name)",
    "org_type": "jenis organisasi: perusahaan, pemerintah, sekolah, rumah sakit, LSM (org type)",
    "sector_text": "sektor / bidang usaha / kategori industri / jenis layanan (sector, industry)",
    "address_raw": "alamat lengkap: jalan, nomor, kelurahan, kecamatan (street address)",
    "city": "kota / kabupaten (city, regency)",
    "province": "provinsi (province)",
    "postal_code": "kode pos (postal code, zip)",
    "website": "situs web / website / URL / domain",
    "phone": "nomor telepon / telp / HP / WhatsApp / fax (phone number)",
    "email": "alamat email / surel (email address)",
    "notes": "catatan / keterangan / hint tambahan (notes, remarks)",
}
FIELDS: tuple[str, ...] = tuple(FIELD_DESCRIPTIONS)

_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_PHONE = re.compile(r"^(\+?62|0)[\d\s().\-/]{6,18}$")
_POSTAL = re.compile(r"^\d{5}$")
_URL = re.compile(
    r"^(https?://|www\.)|^[\w-]+(\.[\w-]+)*\.(id|com|org|net|co\.id|ac\.id|sch\.id|go\.id)(/|$)",
    re.I,
)
_ADDRESS = re.compile(
    r"\b(jl\.?|jln\.?|jalan|no\.?|rt\.?|rw\.?|kel\.?|kec\.?|blok|komplek|gg\.?)\b", re.I
)
_ORG_WORDS = re.compile(
    r"\b(pt|cv|tbk|ud|koperasi|yayasan|sekolah|sma|smk|smp|sd|universitas|rs|rsud|klinik|"
    r"puskesmas|dinas|badan|kantor|perusahaan|pemerintah|swasta|negeri|lsm)\b",
    re.I,
)


@dataclass(frozen=True)
class ColumnProfile:
    header: str
    values: list[str]


@dataclass(frozen=True)
class ColumnMapping:
    source_col: str
    field: str | None  # None -> disimpan ke `extra`
    confidence: float
    method: str = "local"
    pattern: float = 0.0
    semantic: float = 0.0
    alternatives: list[tuple[str, float]] = dc_field(default_factory=list)


def _ratio(values: Sequence[str], pattern: re.Pattern[str]) -> float:
    return sum(1 for v in values if pattern.search(v)) / len(values) if values else 0.0


@dataclass(frozen=True)
class RegionVocab:
    """Nama + alias wilayah (huruf kecil) dari `mcp.regions` untuk mengenali kolom kota/provinsi."""

    cities: frozenset[str] = frozenset()
    provinces: frozenset[str] = frozenset()


NO_REGIONS = RegionVocab()


def pattern_scores(values: Iterable[str], regions: RegionVocab = NO_REGIONS) -> dict[str, float]:
    """Skor 0..1 per field dari pola nilai kolom (tanpa header)."""
    vals = [v.strip() for v in values if v and v.strip()]
    if not vals:
        return dict.fromkeys(FIELDS, 0.0)
    email = _ratio(vals, _EMAIL)
    phone = _ratio([re.sub(r"\s+", " ", v) for v in vals], _PHONE)
    postal = _ratio(vals, _POSTAL)
    url = _ratio(vals, _URL) * (1 - email)
    address = _ratio(vals, _ADDRESS)
    org = _ratio(vals, _ORG_WORDS)
    avg_len = float(np.mean([len(v) for v in vals]))
    distinct = len({v.lower() for v in vals}) / len(vals)
    alpha = float(np.mean([sum(c.isalpha() for c in v) / max(1, len(v)) for v in vals]))
    lowered = [v.lower() for v in vals]
    city_hit = _ratio_set(lowered, regions.cities)
    province_hit = _ratio_set(lowered, regions.provinces)
    is_text = alpha > 0.6 and email < 0.2 and url < 0.2 and phone < 0.2
    name_score = (
        (0.5 + 0.5 * org) * distinct * (1 - address) if is_text and 4 <= avg_len <= 90 else 0.0
    )
    return {
        "email": email,
        "phone": phone,
        "postal_code": postal,
        "website": url,
        "address_raw": address * (1.0 if avg_len > 15 else 0.5),
        "city": city_hit * (1 - address),
        "province": province_hit * (1 - address),
        "name": name_score * (1 - max(city_hit, province_hit)),
        "org_type": org * (1 - distinct) if is_text and avg_len < 25 else 0.0,
        "sector_text": 0.4 * (1 - distinct) if is_text and avg_len < 60 else 0.0,
        "notes": 0.3 if is_text and avg_len > 40 and address < 0.3 else 0.0,
    }


def _ratio_set(values: Sequence[str], vocab: frozenset[str]) -> float:
    if not vocab or not values:
        return 0.0
    return sum(1 for v in values if v in vocab) / len(values)


async def load_region_vocab(db: object) -> RegionVocab:
    """Kosakata wilayah dari `mcp.regions` (nama resmi + alias)."""
    from sqlalchemy import text

    async with db.session() as session:  # type: ignore[attr-defined]
        rows = (await session.execute(text("SELECT level, name, aliases FROM mcp.regions"))).all()
    cities: set[str] = set()
    provinces: set[str] = set()
    for r in rows:
        target = provinces if r.level == "province" else cities
        target.update(n.lower() for n in [r.name, *(r.aliases or [])])
    return RegionVocab(frozenset(cities), frozenset(provinces))


def column_text(col: ColumnProfile) -> str:
    samples = [v.strip()[:SAMPLE_CHARS] for v in col.values if v and v.strip()][:SAMPLE_VALUES]
    return f"Kolom: {col.header}. Contoh: {'; '.join(samples)}"


def assign(
    headers: list[str], combined: np.ndarray, pattern: np.ndarray, semantic: np.ndarray
) -> list[ColumnMapping]:
    """Penugasan Hungarian kolom -> field; sisanya `field=None`."""
    rows, cols = linear_sum_assignment(-combined)
    chosen = {int(r): int(c) for r, c in zip(rows, cols, strict=True)}
    out: list[ColumnMapping] = []
    for i, header in enumerate(headers):
        order = np.argsort(-combined[i])[:3]
        alts = [(FIELDS[int(j)], round(float(combined[i, j]), 3)) for j in order]
        j = chosen.get(i)
        if j is None or combined[i, j] < MIN_CONFIDENCE:
            out.append(
                ColumnMapping(header, None, round(float(combined[i].max()), 3), alternatives=alts)
            )
            continue
        out.append(
            ColumnMapping(
                header,
                FIELDS[j],
                round(float(combined[i, j]), 3),
                pattern=round(float(pattern[i, j]), 3),
                semantic=round(float(semantic[i, j]), 3),
                alternatives=alts,
            )
        )
    return out


async def map_columns(
    columns: list[ColumnProfile],
    embedder: Embedder | None,
    *,
    regions: RegionVocab = NO_REGIONS,
) -> list[ColumnMapping]:
    """Mapping lokal. `embedder=None` -> pola nilai saja (mis. saat embed-local mati)."""
    if not columns:
        return []
    pattern = np.array(
        [[pattern_scores(c.values, regions)[f] for f in FIELDS] for c in columns], dtype=np.float32
    )
    if embedder is None:
        semantic = np.zeros_like(pattern)
        combined = pattern
    else:
        col_vecs = as_matrix(await embedder.embed([column_text(c) for c in columns], "query"))
        field_vecs = as_matrix(await embedder.embed(list(FIELD_DESCRIPTIONS.values()), "passage"))
        semantic = np.clip(cosine(col_vecs, field_vecs), 0.0, 1.0)
        combined = W_PATTERN * pattern + W_SEMANTIC * semantic
    return assign([c.header for c in columns], combined, pattern, semantic)
