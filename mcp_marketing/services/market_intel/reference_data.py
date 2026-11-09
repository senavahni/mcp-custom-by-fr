"""Data referensi global: wilayah Kemendagri + KBLI 2020 + pemetaan ISIC Rev.4 (MCP-V1-02).

Sumber & lisensi: `data/README.md`. `load_reference_data` idempoten (upsert) sehingga dipakai
migrasi `0005_market_intel` dan dapat dijalankan ulang saat data diperbarui. Kolom
`industries.embedding` tidak disentuh (diisi klasifikasi lokal, MCP-V1-04).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

DATA_DIR = Path(__file__).resolve().parent / "data"
REGIONS_CSV = DATA_DIR / "regions.csv"
KBLI_CSV = DATA_DIR / "kbli2020.csv"
ISIC_MAP_CSV = DATA_DIR / "kbli2020_isic4.csv"

# Nama resmi -> sebutan umum yang sering muncul di alamat/listing.
PROVINCE_ALIASES: dict[str, tuple[str, ...]] = {
    "Daerah Khusus Ibukota Jakarta": ("DKI Jakarta", "Jakarta", "DKI"),
    "Daerah Istimewa Yogyakarta": ("DI Yogyakarta", "DIY", "Yogyakarta", "Jogja"),
    "Kepulauan Bangka Belitung": ("Bangka Belitung", "Babel"),
    "Kepulauan Riau": ("Kepri",),
    "Nusa Tenggara Barat": ("NTB",),
    "Nusa Tenggara Timur": ("NTT",),
    "Sumatera Utara": ("Sumut", "Sumatra Utara"),
    "Sumatera Barat": ("Sumbar", "Sumatra Barat"),
    "Sumatera Selatan": ("Sumsel", "Sumatra Selatan"),
    "Jawa Barat": ("Jabar",),
    "Jawa Tengah": ("Jateng",),
    "Jawa Timur": ("Jatim",),
    "Kalimantan Barat": ("Kalbar",),
    "Kalimantan Tengah": ("Kalteng",),
    "Kalimantan Selatan": ("Kalsel",),
    "Kalimantan Timur": ("Kaltim",),
    "Kalimantan Utara": ("Kaltara",),
    "Sulawesi Utara": ("Sulut",),
    "Sulawesi Tengah": ("Sulteng",),
    "Sulawesi Selatan": ("Sulsel",),
    "Sulawesi Tenggara": ("Sultra",),
    "Sulawesi Barat": ("Sulbar",),
}
_REGENCY_PREFIX = re.compile(r"^(Kabupaten|Kota)( Administrasi)? ")


@dataclass(frozen=True)
class LoadCounts:
    regions: int
    industries: int
    isic_map: int


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def region_aliases(name: str, level: str) -> list[str]:
    """Alias pencocokan alamat; nama resmi sendiri tidak dimasukkan."""
    if level == "province":
        return list(PROVINCE_ALIASES.get(name, ()))
    match = _REGENCY_PREFIX.match(name)
    if match is None:
        return []
    bare = name[match.end() :]
    aliases = [bare]
    if match.group(1) == "Kabupaten":
        aliases += [f"Kab. {bare}", f"Kab {bare}"]
    elif match.group(2):  # "Kota Administrasi Jakarta Selatan" -> juga "Kota Jakarta Selatan"
        aliases.append(f"Kota {bare}")
    return aliases


def region_rows() -> list[dict[str, Any]]:
    return [
        {
            "code": r["code"],
            "level": r["level"],
            "parent_code": r["parent_code"] or None,
            "name": r["name"],
            "aliases": region_aliases(r["name"], r["level"]),
            "lat": float(r["centroid_lat"]) if r["centroid_lat"] else None,
            "lng": float(r["centroid_lng"]) if r["centroid_lng"] else None,
        }
        for r in _read(REGIONS_CSV)
    ]


def industry_rows() -> list[dict[str, Any]]:
    """Urut induk dulu (section -> subclass) agar FK parent_code terpenuhi saat insert."""
    order = {"section": 0, "division": 1, "group": 2, "class": 3, "subclass": 4}
    rows = [
        {
            "code": r["kbli_code"],
            "parent_code": r["parent_code"] or None,
            "level": r["level"],
            "name_id": r["name_id"],
            "name_en": r["name_en"] or None,
            "description": r["description"] or None,
        }
        for r in _read(KBLI_CSV)
    ]
    return sorted(rows, key=lambda r: (order[r["level"]], r["code"]))


def isic_rows() -> list[dict[str, Any]]:
    return [
        {
            "kbli": r["kbli_code"],
            "isic": r["isic_code"],
            "name": r["isic_name"],
            "match": r["match_level"],
        }
        for r in _read(ISIC_MAP_CSV)
    ]


_UPSERT_REGION = text(
    """
    INSERT INTO mcp.regions (code, level, parent_code, name, aliases, centroid_lat, centroid_lng)
    VALUES (:code, :level, :parent_code, :name, :aliases, :lat, :lng)
    ON CONFLICT (code) DO UPDATE SET level = EXCLUDED.level, parent_code = EXCLUDED.parent_code,
      name = EXCLUDED.name, aliases = EXCLUDED.aliases, centroid_lat = EXCLUDED.centroid_lat,
      centroid_lng = EXCLUDED.centroid_lng, updated_at = now()
    """
)
_UPSERT_INDUSTRY = text(
    """
    INSERT INTO mcp.industries (kbli_code, parent_code, level, name_id, name_en, description)
    VALUES (:code, :parent_code, :level, :name_id, :name_en, :description)
    ON CONFLICT (kbli_code) DO UPDATE SET parent_code = EXCLUDED.parent_code,
      level = EXCLUDED.level, name_id = EXCLUDED.name_id, name_en = EXCLUDED.name_en,
      description = EXCLUDED.description, updated_at = now()
    """
)
_UPSERT_ISIC = text(
    """
    INSERT INTO mcp.industry_isic_map (kbli_code, isic_code, isic_name, match_level)
    VALUES (:kbli, :isic, :name, :match)
    ON CONFLICT (kbli_code, isic_code) DO UPDATE SET isic_name = EXCLUDED.isic_name,
      match_level = EXCLUDED.match_level
    """
)


def load_reference_data(conn: Connection) -> LoadCounts:
    """Upsert wilayah, KBLI dan pemetaan ISIC (sinkron; dipanggil dari migrasi Alembic)."""
    regions = region_rows()
    provinces = [r for r in regions if r["level"] == "province"]
    conn.execute(_UPSERT_REGION, provinces)
    conn.execute(_UPSERT_REGION, [r for r in regions if r["level"] != "province"])
    industries = industry_rows()
    conn.execute(_UPSERT_INDUSTRY, industries)
    mapping = isic_rows()
    conn.execute(_UPSERT_ISIC, mapping)
    return LoadCounts(regions=len(regions), industries=len(industries), isic_map=len(mapping))
