"""MCP-V1-02: kelengkapan & konsistensi data referensi (wilayah Kemendagri, KBLI 2020, ISIC Rev.4)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from mcp_marketing.core.config import ConfigError
from mcp_marketing.services.kb import vector
from mcp_marketing.services.market_intel import reference_data as ref

REGIONS = ref.region_rows()
INDUSTRIES = ref.industry_rows()
ISIC = ref.isic_rows()


def test_regions_complete_38_provinces_514_regencies() -> None:
    provinces = [r for r in REGIONS if r["level"] == "province"]
    regencies = [r for r in REGIONS if r["level"] == "regency_city"]
    assert len(provinces) == 38
    assert len(regencies) == 514
    codes = {r["code"] for r in REGIONS}
    assert len(codes) == len(REGIONS)
    assert all(r["parent_code"] in codes for r in regencies)
    assert all(re.fullmatch(r"\d{2}(\.\d{2})?", r["code"]) for r in REGIONS)


def test_regions_have_centroids_inside_indonesia() -> None:
    for r in REGIONS:
        assert -11.5 <= r["lat"] <= 6.5, r
        assert 94.5 <= r["lng"] <= 141.5, r
        assert r["name"] == r["name"].strip()


@pytest.mark.parametrize(
    ("name", "level", "expected"),
    [
        ("Kabupaten Bandung", "regency_city", ["Bandung", "Kab. Bandung", "Kab Bandung"]),
        ("Kota Bandung", "regency_city", ["Bandung"]),
        (
            "Kota Administrasi Jakarta Selatan",
            "regency_city",
            ["Jakarta Selatan", "Kota Jakarta Selatan"],
        ),
        ("Daerah Khusus Ibukota Jakarta", "province", ["DKI Jakarta", "Jakarta", "DKI"]),
        ("Jawa Barat", "province", ["Jabar"]),
        ("Aceh", "province", []),
    ],
)
def test_region_aliases(name: str, level: str, expected: list[str]) -> None:
    assert ref.region_aliases(name, level) == expected


def test_kbli_2020_complete_hierarchy() -> None:
    by_level: dict[str, int] = {}
    for r in INDUSTRIES:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    assert by_level["section"] == 21
    assert by_level["subclass"] == 1790  # jumlah kelompok KBLI 2020
    codes = {r["code"] for r in INDUSTRIES}
    seen: set[str] = set()
    for r in INDUSTRIES:  # urutan insert: induk selalu lebih dulu (FK parent_code)
        assert r["parent_code"] is None or r["parent_code"] in seen, r["code"]
        seen.add(r["code"])
    assert seen == codes
    assert all(r["name_id"] for r in INDUSTRIES)


def test_every_kbli_code_mapped_to_isic() -> None:
    mapped = {m["kbli"] for m in ISIC}
    assert mapped == {r["code"] for r in INDUSTRIES}
    for m in ISIC:
        assert m["match"] in ("exact", "class", "group", "division")
        assert re.fullmatch(r"[A-U]|\d{2,4}", m["isic"])
        assert m["kbli"].startswith(m["isic"]) or m["kbli"].isalpha()


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar(self) -> Any:
        return self.value


class _FakeSession:
    def __init__(self, column_type: str | None) -> None:
        self.column_type = column_type

    async def execute(self, *_: Any, **__: Any) -> _FakeResult:
        return _FakeResult(self.column_type)


async def test_ensure_pgvector_rejects_fallback_when_required() -> None:
    vector.reset_mode_cache()
    with pytest.raises(ConfigError, match="KB_REQUIRE_PGVECTOR"):
        await vector.ensure_pgvector(_FakeSession("real[]"), required=True)  # type: ignore[arg-type]
    vector.reset_mode_cache()


async def test_ensure_pgvector_allows_fallback_when_not_required() -> None:
    vector.reset_mode_cache()
    mode = await vector.ensure_pgvector(_FakeSession("real[]"), required=False)  # type: ignore[arg-type]
    assert mode == "array"
    vector.reset_mode_cache()
    mode = await vector.ensure_pgvector(_FakeSession("vector"), required=True)  # type: ignore[arg-type]
    assert mode == "vector"
    vector.reset_mode_cache()


def test_hnsw_1024_index_registered() -> None:
    assert any("kb_chunks_hnsw_1024" in s and "embed_dim = 1024" for s in vector.HNSW_INDEXES)
    assert vector.FIXED_DIM_TABLES["mcp.industries"][0] == 1024
