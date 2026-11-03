"""Statistik Google Trends tanpa LLM: rata-rata, puncak, slope, perubahan, musiman, share."""

from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from datetime import date
from typing import Any

from scipy.stats import linregress

from mcp_marketing.services.collectors.trends_base import InterestResult

MAX_TABLE_ROWS = 26
SEASONALITY_MIN_DAYS = 730


def _series(result: InterestResult, keyword: str) -> list[tuple[date, int, bool]]:
    return [(d, values.get(keyword, 0), partial) for d, values, partial in result.points]


def keyword_stats(
    series: list[tuple[date, int, bool]], prev: list[tuple[date, int, bool]] | None
) -> dict[str, Any]:
    if not series:
        return {
            "avg": 0.0,
            "peak": None,
            "slope_per_week": 0.0,
            "change_pct_vs_prev_period": None,
            "is_partial_last": False,
        }
    values = [v for _, v, _ in series]
    peak_date, peak_value, _ = max(series, key=lambda s: (s[1], s[0]))
    slope = 0.0
    if len(series) >= 2 and len(set(values)) > 1:
        x = [(d - series[0][0]).days for d, _, _ in series]
        slope = float(linregress(x, values).slope) * 7
    avg = statistics.fmean(values)
    change = None
    if prev:
        prev_avg = statistics.fmean(v for _, v, _ in prev)
        change = round((avg - prev_avg) / prev_avg * 100, 1) if prev_avg > 0 else None
    out: dict[str, Any] = {
        "avg": round(avg, 2),
        "peak": {"date": peak_date.isoformat(), "value": peak_value},
        "slope_per_week": round(slope, 3),
        "change_pct_vs_prev_period": change,
        "is_partial_last": series[-1][2],
    }
    if (series[-1][0] - series[0][0]).days >= SEASONALITY_MIN_DAYS:
        by_month: dict[int, list[int]] = defaultdict(list)
        for d, v, _ in series:
            by_month[d.month].append(v)
        best = max(by_month, key=lambda m: statistics.fmean(by_month[m]))
        out["seasonality_hint"] = calendar.month_name[best]
    return out


def summarize(result: InterestResult, prev: InterestResult | None = None) -> dict[str, Any]:
    per_keyword = {
        k: keyword_stats(_series(result, k), _series(prev, k) if prev else None)
        for k in result.keywords
    }
    total = sum(s["avg"] for s in per_keyword.values()) or 1.0
    share = {k: round(s["avg"] / total * 100, 1) for k, s in per_keyword.items()}
    return {"per_keyword": per_keyword, "share_of_search": share, "resolution": result.resolution}


def resample_table(
    result: InterestResult, max_rows: int = MAX_TABLE_ROWS
) -> tuple[dict[str, Any], str]:
    """Tabel ringkas: mingguan (≤ 26 baris), bila lebih panjang -> bulanan."""

    def bucket(d: date, granularity: str) -> str:
        if granularity == "week":
            iso = d.isocalendar()
            return date.fromisocalendar(iso.year, iso.week, 1).isoformat()
        return d.strftime("%Y-%m")

    for granularity in ("week", "month"):
        groups: dict[str, list[dict[str, int]]] = defaultdict(list)
        for d, values, _ in result.points:
            groups[bucket(d, granularity)].append(values)
        if len(groups) <= max_rows or granularity == "month":
            keys = sorted(groups)[-max_rows:]
            rows = [
                [
                    k,
                    *(
                        round(statistics.fmean(v.get(kw, 0) for v in groups[k]), 1)
                        for kw in result.keywords
                    ),
                ]
                for k in keys
            ]
            return {"cols": [granularity, *result.keywords], "rows": rows}, granularity
    raise AssertionError("unreachable")  # pragma: no cover


def scale_with_anchor(groups: list[InterestResult], anchor: str) -> InterestResult:
    """Gabungkan permintaan bergrup (tiap grup memuat `anchor`) ke skala grup pertama."""
    base = groups[0]
    base_anchor = statistics.fmean(v.get(anchor, 0) for _, v, _ in base.points) or 1.0
    merged: dict[date, dict[str, float]] = {d: dict(v) for d, v, _ in base.points}
    partial = {d: p for d, _, p in base.points}
    keywords = list(base.keywords)
    for group in groups[1:]:
        group_anchor = statistics.fmean(v.get(anchor, 0) for _, v, _ in group.points) or 1.0
        factor = base_anchor / group_anchor
        for d, values, p in group.points:
            row = merged.setdefault(d, {})
            partial[d] = partial.get(d, False) or p
            for kw, val in values.items():
                if kw != anchor:
                    row[kw] = val * factor
        keywords += [k for k in group.keywords if k != anchor and k not in keywords]
    points = [
        (d, {k: int(round(v)) for k, v in merged[d].items()}, partial.get(d, False))
        for d in sorted(merged)
    ]
    return InterestResult(
        keywords=keywords,
        points=points,
        resolution=base.resolution,
        provider=f"{base.provider}+anchor",
    )
