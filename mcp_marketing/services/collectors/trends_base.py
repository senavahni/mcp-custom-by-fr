"""Model, antarmuka provider, dan parser Google Trends (JSON widget & CSV unduhan)."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

import orjson

from mcp_marketing.core.errors import UpstreamError

Gprop = Literal["web", "news", "images", "youtube", "froogle"]
BREAKOUT_VALUE = 5_000
_LT1 = "<1"


@dataclass
class InterestResult:
    keywords: list[str]
    # (tanggal bucket, {keyword: nilai 0-100}, is_partial)
    points: list[tuple[date, dict[str, int], bool]]
    resolution: str | None = None
    provider: str = ""


@dataclass
class RegionValue:
    region: str
    value: int
    geo_code: str | None = None


@dataclass
class RelatedItem:
    term: str
    value: int
    position: int


@dataclass
class RelatedResult:
    top: list[RelatedItem] = field(default_factory=list)
    rising: list[RelatedItem] = field(default_factory=list)


class TrendsProvider(Protocol):
    name: str

    async def interest_over_time(
        self, keywords: list[str], start: date, end: date, geo: str, category: int, gprop: str
    ) -> InterestResult: ...

    async def interest_by_region(
        self, keyword: str, start: date, end: date, geo: str, resolution: str
    ) -> list[RegionValue]: ...

    async def related(
        self, keyword: str, start: date, end: date, geo: str, kind: str
    ) -> RelatedResult: ...


class TrendsRateLimited(UpstreamError):
    """HTTP 429 dari Google Trends (retry dengan backoff, lalu fallback provider)."""


def strip_xssi(text: str) -> Any:
    """Respons Trends diawali `)]}'` (anti-XSSI); ambil JSON mulai `{`."""
    start = text.find("{")
    if start < 0:
        raise UpstreamError("Respons Google Trends bukan JSON", details={"head": text[:80]})
    return orjson.loads(text[start:])


def resolution_of(dates: list[date]) -> str | None:
    if len(dates) < 2:
        return None
    gap = (dates[1] - dates[0]).days
    return "day" if gap <= 1 else "week" if gap <= 7 else "month"


def _num(value: Any) -> int:
    if isinstance(value, int | float):
        return int(value)
    text = str(value).strip()
    if text == _LT1:
        return 0
    if text.lower() in ("breakout", "lonjakan", "melonjak"):
        return BREAKOUT_VALUE
    text = text.replace("%", "").replace("+", "").replace(",", "").replace(".", "")
    return int(text) if text.lstrip("-").isdigit() else 0


def parse_multiline(body: dict[str, Any], keywords: list[str]) -> InterestResult:
    data = (body.get("default") or {}).get("timelineData") or []
    points = []
    for row in data:
        ts = datetime.fromtimestamp(int(row["time"]), tz=UTC).date()
        values = {kw: _num(v) for kw, v in zip(keywords, row.get("value") or [], strict=False)}
        points.append((ts, values, bool(row.get("isPartial"))))
    return InterestResult(
        keywords=keywords, points=points, resolution=resolution_of([p[0] for p in points])
    )


def parse_geo(body: dict[str, Any]) -> list[RegionValue]:
    rows = (body.get("default") or {}).get("geoMapData") or []
    out = [
        RegionValue(
            region=str(r.get("geoName")),
            value=_num((r.get("value") or [0])[0]),
            geo_code=r.get("geoCode"),
        )
        for r in rows
        if r.get("hasData", [True])
    ]
    return sorted(out, key=lambda r: (-r.value, r.region))


def parse_related(body: dict[str, Any], kind: str) -> RelatedResult:
    lists = (body.get("default") or {}).get("rankedList") or []

    def items(idx: int) -> list[RelatedItem]:
        if idx >= len(lists):
            return []
        ranked = lists[idx].get("rankedKeyword") or []
        out = []
        for pos, r in enumerate(ranked, 1):
            term = r.get("query") if kind == "queries" else (r.get("topic") or {}).get("title")
            value = (
                BREAKOUT_VALUE
                if str(r.get("formattedValue", "")).lower() in ("breakout", "lonjakan")
                else _num(r.get("value", 0))
            )
            if term:
                out.append(RelatedItem(term=str(term), value=value, position=pos))
        return out

    return RelatedResult(top=items(0), rising=items(1))


# --------------------------------------------------------------------- CSV unduhan manual
_DATE_HEADERS = ("week", "day", "month", "minggu", "hari", "bulan")
_REGION_HEADERS = ("region", "city", "subregion", "wilayah", "kota", "subwilayah", "metro")


def _kw_label(header: str) -> str:
    return re.sub(r":\s*\(.*\)\s*$", "", header).strip()


def parse_trends_csv(text: str) -> dict[str, Any]:
    """Parse CSV unduhan trends.google.com (label EN/ID, nilai '<1').

    Kembalikan `{"kind": "interest"|"region"|"related", ...}`.
    """
    lines = [ln for ln in text.replace("﻿", "").splitlines()]
    reader = list(csv.reader(io.StringIO("\n".join(lines))))
    header_idx = next(
        (
            i
            for i, r in enumerate(reader)
            if r and r[0].strip().lower() in _DATE_HEADERS + _REGION_HEADERS
        ),
        None,
    )
    if header_idx is not None:
        header = reader[header_idx]
        keywords = [_kw_label(h) for h in header[1:]]
        rows = [r for r in reader[header_idx + 1 :] if r and r[0].strip()]
        if header[0].strip().lower() in _DATE_HEADERS:
            points = [
                (
                    date.fromisoformat(r[0].strip()[:10]),
                    {k: _num(v) for k, v in zip(keywords, r[1:], strict=False)},
                    False,
                )
                for r in rows
            ]
            return {
                "kind": "interest",
                "result": InterestResult(
                    keywords, points, resolution_of([p[0] for p in points]), "csv"
                ),
            }
        regions = [
            RegionValue(region=r[0].strip(), value=_num(r[1]) if len(r) > 1 and r[1] else 0)
            for r in rows
        ]
        return {"kind": "region", "keyword": keywords[0] if keywords else "", "result": regions}
    related = RelatedResult()
    section: list[RelatedItem] | None = None
    for r in reader:
        if not r or not r[0].strip():
            continue
        label = r[0].strip().upper()
        if label in ("TOP", "TERATAS", "TERPOPULER"):
            section = related.top
            continue
        if label in ("RISING", "MENINGKAT", "NAIK DAUN"):
            section = related.rising
            continue
        if section is not None and len(r) >= 2:
            section.append(
                RelatedItem(term=r[0].strip(), value=_num(r[1]), position=len(section) + 1)
            )
    return {"kind": "related", "result": related}
