"""Tahap Analisis job `intel`: gabung item semua sumber, sentimen, statistik tanpa LLM, sampel.

Fungsi murni (tanpa I/O) dipisah dari pembacaan DB agar mudah diuji (tests/unit/test_intel_*).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from mcp_marketing.models import ResultItem
from mcp_marketing.models.browser import WebPage
from mcp_marketing.services.nlp import aggregate
from mcp_marketing.services.nlp.sentiment import Item, Outcome
from mcp_marketing.services.results.result_store import Owner

MAX_ITEMS = 10_000
WEB_TEXT_CHARS = 2_000
TEXT_CHARS = 280
TOP_N = 10
EXAMPLES = 3
SAMPLE_MAX = 60
SAMPLE_MIN_PER_LABEL = 5
ITEM_COLS = [
    "id",
    "source",
    "posted_at",
    "author",
    "text",
    "url",
    "likes",
    "replies",
    "reposts",
    "engagement",
    "sentiment",
    "score",
    "aspects",
    "hashtags",
]
ITEMS_PAGE_COLS = [
    "id",
    "source",
    "posted_at",
    "author",
    "text",
    "url",
    "engagement",
    "sentiment",
    "score",
]
SOCIAL_KINDS = ("x_search", "threads_search")
WEB_KINDS = ("web_scrape", "web_crawl")


def engagement(row: dict[str, Any]) -> int:
    return sum(int(row.get(k) or 0) for k in ("likes", "replies", "reposts"))


def social_item(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(data["id"]),
        "source": data.get("source"),
        "posted_at": data.get("posted_at"),
        "author": data.get("author"),
        "text": data.get("text") or "",
        "url": data.get("url"),
        "likes": int(data.get("likes") or 0),
        "replies": int(data.get("replies") or 0),
        "reposts": int(data.get("reposts") or 0),
        "hashtags": list(data.get("hashtags") or []),
    }


def web_item(page_id: str, data: dict[str, Any], content: str | None) -> dict[str, Any] | None:
    title = data.get("title") or ""
    body = (content or "").strip()
    if data.get("status") != "ok" or not (title or body):
        return None
    text = f"{title}\n{body}".strip()[:WEB_TEXT_CHARS]
    return {
        "id": page_id,
        "source": "web",
        "posted_at": data.get("published_at"),
        "author": None,
        "text": text,
        "url": data.get("url"),
        "likes": 0,
        "replies": 0,
        "reposts": 0,
        "hashtags": [],
    }


async def load_items(services: Any, owner: Owner, kind: str, handle: str) -> list[dict[str, Any]]:
    """Item terpadu dari handle hasil job anak (social_posts / web_pages)."""
    async with services.db.session() as session:
        rs = await services.results.get_set(session, handle, owner)
        rows = (
            await session.execute(
                select(ResultItem.ref_id, ResultItem.data)
                .where(ResultItem.handle == rs.handle)
                .order_by(ResultItem.idx)
                .limit(MAX_ITEMS)
            )
        ).all()
        if kind in SOCIAL_KINDS:
            return [social_item(dict(r.data)) for r in rows]
        ids = [r.ref_id for r in rows if r.ref_id]
        pages = dict(
            (
                await session.execute(
                    select(WebPage.id, WebPage.content_md).where(WebPage.id.in_(ids))
                )
            ).all()
        )
    out = []
    for r in rows:
        item = web_item(str(r.ref_id), dict(r.data), pages.get(r.ref_id)) if r.ref_id else None
        if item is not None:
            out.append(item)
    return out


def to_sentiment_items(items: list[dict[str, Any]]) -> list[Item]:
    return [Item(key=i["id"], text=i["text"]) for i in items if i["text"].strip()]


def label_rows(items: list[dict[str, Any]], outcome: Outcome | None) -> list[dict[str, Any]]:
    """Item + label sentimen (`label` untuk modul aggregate, `sentiment` untuk kolom kontrak)."""
    rows = []
    for item in items:
        verdict = outcome.verdicts.get(item["id"]) if outcome else None
        row = {**item, "engagement": engagement(item)}
        if verdict is not None:
            row.update(
                label=verdict.label,
                sentiment=verdict.label,
                score=verdict.score,
                aspects=verdict.aspects,
            )
        else:
            row.update(label=None, sentiment=None, score=None, aspects=[])
        rows.append(row)
    return rows


def _day(value: Any, tz: ZoneInfo) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.astimezone(tz).date().isoformat() if dt.tzinfo else dt.date().isoformat()


def volume_by_day(rows: list[dict[str, Any]], tz: str) -> dict[str, Any]:
    zone = ZoneInfo(tz)
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        day = _day(r.get("posted_at"), zone)
        if day:
            counts[(day, str(r.get("source")))] += 1
    return {
        "cols": ["day", "source", "count"],
        "rows": [[d, s, c] for (d, s), c in sorted(counts.items())],
    }


def top_pairs(values: list[str], n: int = TOP_N) -> list[list[Any]]:
    return [[v, c] for v, c in Counter(values).most_common(n)]


def examples(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    picked = sorted(
        (r for r in rows if r.get("label") == label and r.get("text")),
        key=lambda r: (-r["engagement"], -float(r.get("score") or 0)),
    )[:EXAMPLES]
    return [
        {
            "id": r["id"],
            "source": r["source"],
            "author": r.get("author"),
            "text": str(r["text"])[:TEXT_CHARS],
            "url": r.get("url"),
            "created_at": r.get("posted_at"),
        }
        for r in picked
    ]


def sentiment_summary(rows: list[dict[str, Any]], tz: str) -> dict[str, Any] | None:
    labeled = [r for r in rows if r.get("label")]
    if not labeled:
        return None
    dist = aggregate.distribution(labeled)
    total = dist["total"]
    return {
        "distribution": {k: round(v / total, 4) for k, v in dist["counts"].items()},
        "net_sentiment": dist["net_sentiment"],
        "by_day": aggregate.by_group([r for r in labeled if r.get("posted_at")], "day", tz),
        "aspects": aggregate.aspects_table(labeled),
        "examples": {
            "positive": examples(labeled, "positive"),
            "negative": examples(labeled, "negative"),
        },
    }


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_source = Counter(str(r["source"]) for r in rows)
    return {"items": len(rows), "by_source": dict(sorted(by_source.items()))}


def stratified_sample(rows: list[dict[str, Any]], limit: int = SAMPLE_MAX) -> list[dict[str, Any]]:
    """Sampel untuk sintesis LLM (T5): proporsional per label, engagement tertinggi lebih dulu."""
    by_label: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: -r["engagement"]):
        by_label.setdefault(str(r.get("label")), []).append(r)
    total = len(rows) or 1
    quota = {
        k: max(SAMPLE_MIN_PER_LABEL, round(limit * len(v) / total)) for k, v in by_label.items()
    }
    picked: list[dict[str, Any]] = []
    for k, v in by_label.items():
        picked.extend(v[: quota[k]])
    return sorted(picked, key=lambda r: -r["engagement"])[:limit]


def stats_for_prompt(
    rows: list[dict[str, Any]], sentiment: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "totals": totals(rows),
        "sentiment": (
            {"distribution": sentiment["distribution"], "net": sentiment["net_sentiment"]}
            if sentiment
            else None
        ),
        "top_hashtags": top_pairs([h for r in rows for h in r.get("hashtags") or []]),
        "aspects": sentiment.get("aspects") if sentiment else None,
    }
