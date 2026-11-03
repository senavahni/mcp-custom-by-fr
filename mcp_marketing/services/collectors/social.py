"""Normalisasi & penyimpanan post sosial (X, Threads) + statistik tanpa LLM (TASK-15/16).

Semua adapter menghasilkan `PostIn`; `store_posts` meng-upsert `social_posts` by
`(source, external_id)` (metrik diperbarui bila lebih baru) dan mencatat `job_posts` +
`matched_keywords`. `build_result` menghitung statistik dari DB sehingga resume tidak menggandakan.
"""

from __future__ import annotations

import re
import statistics
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from mcp_marketing.models.social import JobPost, SocialPost
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.text import truncate
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

HASHTAG_RE = re.compile(r"(?<![\w#])#([\wÀ-ɏ]{2,100})", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.]{1,50})")
POST_COLS = [
    "id",
    "source",
    "posted_at",
    "author",
    "text",
    "likes",
    "replies",
    "reposts",
    "views",
    "hashtags",
    "keywords",
    "url",
]
TEXT_MAX = 280
TOP_N = 10


@dataclass
class PostIn:
    source: str
    external_id: str
    text: str
    posted_at: datetime
    author_handle: str | None = None
    author_id: str | None = None
    author_followers: int | None = None
    url: str | None = None
    lang: str | None = None
    is_reply: bool = False
    is_repost: bool = False
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    views: int | None = None
    media_type: str | None = None
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    provider: str = "browser"
    raw: dict[str, Any] | None = None


def hashtags_of(text_value: str, extra: list[str] | None = None) -> list[str]:
    tags = {t.lower() for t in HASHTAG_RE.findall(text_value)}
    tags |= {t.lower().lstrip("#") for t in extra or [] if t}
    return sorted(tags)


def mentions_of(text_value: str) -> list[str]:
    return sorted({m.lower() for m in MENTION_RE.findall(text_value)})


def matched_keywords(post: PostIn, keywords: list[str]) -> list[str]:
    """Pencocokan case-insensitive di teks & hashtag (keyword `#tag` juga cocok dengan hashtag)."""
    haystack = post.text.lower()
    tags = set(post.hashtags)
    out = []
    for kw in keywords:
        k = kw.lower().strip()
        if k and (k in haystack or k.lstrip("#").replace(" ", "") in tags):
            out.append(kw)
    return out


def passes_filters(
    post: PostIn,
    *,
    start: datetime,
    end: datetime,
    lang: str | None = None,
    min_likes: int | None = None,
    exclude_retweets: bool = False,
    exclude_replies: bool = False,
) -> bool:
    if not (start <= post.posted_at <= end):
        return False
    if lang and post.lang and post.lang.split("-")[0] != lang:
        return False
    if min_likes is not None and (post.likes or 0) < min_likes:
        return False
    if exclude_retweets and post.is_repost:
        return False
    return not (exclude_replies and post.is_reply)


async def store_posts(
    job: JobCtx, posts: list[PostIn], keywords: list[str], *, keep_raw: bool = False
) -> int:
    """Upsert post + relasi job; kembalikan jumlah post BARU untuk job ini."""
    if not posts:
        return 0
    added = 0
    async with job.services.db.session() as session:
        for post in posts:
            values = {
                "source": post.source,
                "external_id": post.external_id[:64],
                "url": post.url,
                "author_handle": (post.author_handle or None) and post.author_handle[:100],
                "author_id": post.author_id,
                "author_followers": post.author_followers,
                "text": post.text,
                "lang": post.lang,
                "posted_at": post.posted_at,
                "is_reply": post.is_reply,
                "is_repost": post.is_repost,
                "likes": post.likes or 0,
                "replies": post.replies or 0,
                "reposts": post.reposts or 0,
                "quotes": post.quotes or 0,
                "views": post.views,
                "hashtags": post.hashtags or hashtags_of(post.text),
                "mentions": post.mentions or mentions_of(post.text),
                "media_type": post.media_type,
                "provider": post.provider,
                "raw": post.raw if keep_raw else None,
                "first_seen_job": job.job_id,
            }
            base = insert(SocialPost).values(**values)
            stmt = base.on_conflict_do_update(
                index_elements=["source", "external_id"],
                set_={
                    k: base.excluded[k]
                    for k in ("likes", "replies", "reposts", "quotes", "views", "text")
                }
                | {"collected_at": func.now()},
            ).returning(SocialPost.id)
            post_id = (await session.execute(stmt)).scalar_one()
            link = insert(JobPost).values(
                job_id=job.job_id,
                post_id=post_id,
                matched_keywords=matched_keywords(post, keywords) or keywords[:1],
            )
            res = await session.execute(
                link.on_conflict_do_nothing(index_elements=["job_id", "post_id"])
            )
            added += int(res.rowcount or 0)  # type: ignore[attr-defined]
    return added


async def job_post_count(job: JobCtx) -> int:
    async with job.services.db.session() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(JobPost).where(JobPost.job_id == job.job_id)
            )
            or 0
        )


def _p90(values: list[int]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=10, method="inclusive")[8])


def compute_stats(rows: list[dict[str, Any]], tz: str) -> dict[str, Any]:
    zone = ZoneInfo(tz)
    per_day: dict[str, list[int]] = {}
    authors: dict[str, dict[str, Any]] = {}
    tags: Counter[str] = Counter()
    for r in rows:
        day = r["posted_at"].astimezone(zone).date().isoformat()
        bucket = per_day.setdefault(day, [0, 0])
        bucket[0] += 1
        bucket[1] += int(r["likes"] or 0)
        if r["author"]:
            a = authors.setdefault(
                r["author"], {"handle": r["author"], "posts": 0, "followers": r.get("followers")}
            )
            a["posts"] += 1
        tags.update(r["hashtags"] or [])
    likes = [int(r["likes"] or 0) for r in rows]
    reposts = [int(r["reposts"] or 0) for r in rows]
    days = sorted(per_day)
    peak = max(days, key=lambda d: per_day[d][0]) if days else None
    return {
        "per_day": {"cols": ["day", "posts", "likes"], "rows": [[d, *per_day[d]] for d in days]},
        "peak_day": {"day": peak, "posts": per_day[peak][0]} if peak else None,
        "top_hashtags": [[t, c] for t, c in tags.most_common(TOP_N)],
        "top_authors": sorted(authors.values(), key=lambda a: (-a["posts"], a["handle"]))[:TOP_N],
        "engagement": {
            "avg_likes": round(statistics.fmean(likes), 2) if likes else 0.0,
            "p90_likes": round(_p90(likes), 2),
            "avg_reposts": round(statistics.fmean(reposts), 2) if reposts else 0.0,
        },
    }


async def build_result(
    job: JobCtx,
    *,
    source: str,
    provider: str,
    keywords: list[str],
    period: tuple[str, str],
    external_units: int = 0,
    usd: float = 0.0,
    notes: list[str] | None = None,
    partial: bool = False,
) -> JobResult:
    sql = text(
        """
        SELECT p.id, p.source, p.posted_at, p.author_handle AS author,
               p.author_followers AS followers,
               p.text, p.likes, p.replies, p.reposts, p.views, p.hashtags, p.url,
               jp.matched_keywords AS keywords
        FROM mcp.job_posts jp JOIN mcp.social_posts p ON p.id = jp.post_id
        WHERE jp.job_id = :job ORDER BY p.posted_at
        """
    )
    async with job.services.db.session() as session:
        rows = [dict(r._mapping) for r in (await session.execute(sql, {"job": job.job_id})).all()]
    stats = compute_stats(rows, job.services.settings.timezone)
    stats.update(provider=provider, cost={"external_units": external_units, "usd": round(usd, 4)})
    if notes:
        stats["notes"] = notes
    owner = Owner(client_id=job.client_id, run_id=job.run_id)
    data_rows = [{**r, "id": str(r["id"])} for r in rows]
    rs = await job.services.results.create(
        kind="social_posts",
        columns=POST_COLS,
        rows=data_rows,
        owner=owner,
        job_id=job.job_id,
        ref_id="id",
        sort_ts="posted_at",
        sort_num="likes",
        stats=stats,
        source_ref={
            "table": "social_posts",
            "job_id": job.job_id,
            "source": source,
            "keywords": keywords,
        },
    )
    top = sorted(rows, key=lambda r: (int(r["likes"] or 0), int(r["reposts"] or 0)), reverse=True)[
        :5
    ]
    peak = stats.get("peak_day") or {}
    label = "X" if source == "x" else "Threads"
    summary = f"{len(rows)} post {label} untuk {', '.join(keywords)[:80]} ({period[0]}–{period[1]})"
    if peak:
        summary += f"; puncak {peak['day']} ({peak['posts']})"
    if partial:
        summary += " [parsial]"
    result = {
        "summary": truncate(summary, 300),
        "handle": rs.handle,
        "total": rs.total,
        "stats": stats,
        "items": {
            "cols": ["posted_at", "author", "text", "likes", "reposts", "url"],
            "rows": [
                [
                    r["posted_at"].isoformat(),
                    r["author"],
                    truncate(r["text"], TEXT_MAX),
                    r["likes"],
                    r["reposts"],
                    r["url"],
                ]
                for r in top
            ],
        },
        "cost": {"llm_tokens": 0, "external_units": external_units, "cache_hit": False},
    }
    if partial:
        result["partial"] = True
    return JobResult(result=result, result_handle=rs.handle)


def new_uuid() -> str:
    return str(uuid.uuid4())
