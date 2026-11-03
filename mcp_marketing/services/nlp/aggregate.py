"""Agregasi sentimen di Python (tanpa LLM): distribusi, net sentiment, per grup, aspek, contoh."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

LABELS = ("positive", "neutral", "negative")
EXAMPLES = 3
EXAMPLE_CHARS = 280
MAX_GROUPS = 50


def _net(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    return round((counts["positive"] - counts["negative"]) / total, 3) if total else 0.0


def distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {label: 0 for label in LABELS}
    for row in rows:
        counts[row["label"]] += 1
    total = sum(counts.values())
    pct = {k: round(v / total * 100, 1) if total else 0.0 for k, v in counts.items()}
    return {"counts": counts, "pct": pct, "total": total, "net_sentiment": _net(counts)}


def _group_keys(row: dict[str, Any], group_by: str, tz: ZoneInfo) -> list[str]:
    if group_by == "day":
        raw = row.get("posted_at")
        if not raw:
            return ["-"]
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return [dt.astimezone(tz).date().isoformat() if dt.tzinfo else dt.date().isoformat()]
    if group_by == "keyword":
        return [str(k) for k in (row.get("keywords") or ["-"])]
    return [str(row.get(group_by) or "-")]


def by_group(rows: list[dict[str, Any]], group_by: str, tz: str) -> dict[str, Any]:
    zone = ZoneInfo(tz)
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {label: 0 for label in LABELS})
    for row in rows:
        for key in _group_keys(row, group_by, zone):
            groups[key][row["label"]] += 1
    ordered = (
        sorted(groups)
        if group_by == "day"
        else sorted(groups, key=lambda k: -sum(groups[k].values()))
    )
    return {
        "cols": [group_by, "total", *LABELS, "net"],
        "rows": [
            [k, sum(groups[k].values()), *(groups[k][lb] for lb in LABELS), _net(groups[k])]
            for k in ordered[:MAX_GROUPS]
        ],
    }


def aspects_table(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {label: 0 for label in LABELS})
    for row in rows:
        for item in row.get("aspects") or []:
            groups[item["aspect"]][item["label"]] += 1
    if not groups:
        return None
    ordered = sorted(groups, key=lambda k: -sum(groups[k].values()))
    return {
        "cols": ["aspect", "mentions", *LABELS, "net"],
        "rows": [
            [k, sum(groups[k].values()), *(groups[k][lb] for lb in LABELS), _net(groups[k])]
            for k in ordered
        ],
    }


def _engagement(row: dict[str, Any]) -> int:
    return sum(int(row.get(k) or 0) for k in ("likes", "replies", "reposts"))


def examples(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for label in ("positive", "negative"):
        picked = sorted(
            (r for r in rows if r["label"] == label and r.get("text")),
            key=lambda r: (-_engagement(r), -float(r.get("score") or 0)),
        )[:EXAMPLES]
        out[label] = [
            {
                "text": str(r["text"])[:EXAMPLE_CHARS],
                "author": r.get("author"),
                "engagement": _engagement(r),
            }
            for r in picked
        ]
    return out


def compare(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    return {
        "net_sentiment_delta": round(current["net_sentiment"] - previous["net_sentiment"], 3),
        "pct_delta": {k: round(current["pct"][k] - previous["pct"][k], 1) for k in LABELS},
        "total_delta": current["total"] - previous["total"],
    }
