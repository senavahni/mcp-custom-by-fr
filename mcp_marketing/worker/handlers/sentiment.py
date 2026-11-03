"""Job `sentiment_analyze` (TASK-20): analisis + agregasi + handle per item."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.models.results import ResultItem
from mcp_marketing.models.social import SocialPost
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.nlp import aggregate
from mcp_marketing.services.nlp.sentiment import Item, Outcome, SentimentAnalyzer
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

MAX_ITEMS = 10_000
ITEM_COLS = [
    "id",
    "label",
    "score",
    "method",
    "lang",
    "aspects",
    "posted_at",
    "source",
    "keywords",
    "author",
    "likes",
    "replies",
    "reposts",
    "text",
]
TEXT_CHARS = 280


async def load_items(services: Any, owner: Owner, source: dict[str, Any]) -> list[Item]:
    if source.get("texts"):
        return [Item(key=str(i), text=t) for i, t in enumerate(source["texts"]) if t.strip()]
    handle = source.get("handle")
    if not handle:
        raise ValidationFailed("source butuh handle atau texts", details={"field": "source"})
    async with services.db.session() as session:
        rs = await services.results.get_set(session, handle, owner)
        if rs.kind != "social_posts":
            raise ValidationFailed("handle harus hasil x_search/threads_search (social_posts)")
        rows = (
            await session.execute(
                select(ResultItem.ref_id, ResultItem.data)
                .where(ResultItem.handle == rs.handle)
                .order_by(ResultItem.idx)
                .limit(MAX_ITEMS)
            )
        ).all()
        ids = [r.ref_id for r in rows if r.ref_id]
        full = dict(
            (
                await session.execute(
                    select(SocialPost.id, SocialPost.text).where(SocialPost.id.in_(ids))
                )
            ).all()
        )
    items = []
    for r in rows:
        data = dict(r.data)
        body = full.get(r.ref_id) or data.get("text") or ""
        if body.strip():
            items.append(
                Item(
                    key=str(r.ref_id or data.get("id")),
                    text=body,
                    target_type="social_post",
                    target_id=r.ref_id,
                    meta=data,
                )
            )
    return items


def item_rows(items: list[Item], outcome: Outcome) -> list[dict[str, Any]]:
    rows = []
    for it in items:
        v = outcome.verdicts[it.key]
        meta = it.meta
        rows.append(
            {
                "id": it.key,
                "label": v.label,
                "score": v.score,
                "method": v.method,
                "lang": v.lang,
                "aspects": v.aspects,
                "posted_at": meta.get("posted_at"),
                "source": meta.get("source"),
                "keywords": meta.get("keywords"),
                "author": meta.get("author"),
                "likes": meta.get("likes"),
                "replies": meta.get("replies"),
                "reposts": meta.get("reposts"),
                "text": it.text[:TEXT_CHARS],
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]], group_by: str | None, tz: str) -> dict[str, Any]:
    dist = aggregate.distribution(rows)
    out: dict[str, Any] = {
        "distribution": dist["pct"],
        "counts": dist["counts"],
        "net_sentiment": dist["net_sentiment"],
        "total": dist["total"],
        "examples": aggregate.examples(rows),
    }
    if group_by:
        out["by_group"] = aggregate.by_group(rows, group_by, tz)
    table = aggregate.aspects_table(rows)
    if table:
        out["aspects"] = table
    return out


class SentimentAnalyzeHandler:
    kind = "sentiment_analyze"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        n = len((params.get("source") or {}).get("texts") or []) or 1_000
        return JobEstimate(seconds=max(5, n // 30))

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        owner = Owner(client_id=job.client_id, run_id=job.run_id)
        items = await load_items(job.services, owner, p["source"])
        if not items:
            raise ValidationFailed("Tidak ada teks untuk dianalisis")
        analyzer = SentimentAnalyzer(
            job.services, run_cpu=job.run_cpu if job.executor is not None else None
        )
        outcome = await analyzer.analyze(
            items,
            method=p.get("method", "auto"),
            language=p.get("language", "auto"),
            aspects=p.get("aspects"),
        )
        rows = item_rows(items, outcome)
        result = summarize(rows, p.get("group_by"), job.services.settings.timezone)
        rs = await job.services.results.create(
            kind="sentiment",
            columns=ITEM_COLS,
            rows=rows,
            owner=owner,
            job_id=job.job_id,
            stats={"distribution": result["distribution"], "net": result["net_sentiment"]},
            source_ref={"tool": "sentiment_analyze", "source": p["source"].get("handle")},
        )
        dist = result["distribution"]
        summary = (
            f"{len(rows)} teks: {dist['positive']}% positif, {dist['neutral']}% netral, "
            f"{dist['negative']}% negatif (net {result['net_sentiment']:+.2f})"
        )
        cost = outcome.cost
        payload = {
            "summary": summary,
            **result,
            "notes": outcome.notes or None,
            "cost": {
                "llm_tokens": cost["llm_tokens"],
                "external_units": 0,
                "cache_hit": cost["cache_hits"] == len(rows),
                "items_model": cost["items_model"],
                "items_llm": cost["items_llm"],
                "cache_hits": cost["cache_hits"],
            },
        }
        return JobResult(result=payload, result_handle=rs.handle)
