"""Sumber ingest KB -> dokumen: text, url, handle (web_pages / social_posts), report, file_text.

Post sosial TIDAK dijadikan 1 post = 1 dokumen: dikelompokkan per (hari, kata kunci) menjadi
dokumen ringkas agar chunk bermakna dan jumlah embedding kecil.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.models.browser import WebPage
from mcp_marketing.models.reports import AnalysisReport
from mcp_marketing.models.results import ResultItem
from mcp_marketing.services.kb.store import DocInput
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.crypto import sha256_hex

SourceType = Literal["text", "url", "handle", "report", "file_text"]
UrlFetcher = Callable[[str], Awaitable[tuple[str | None, str]]]
MAX_HANDLE_DOCS = 500
POST_TEXT_CHARS = 280
MAX_POSTS_PER_DOC = 200


def _title_from(text: str, fallback: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:150]
    return fallback


def text_doc(value: str, metadata: dict[str, Any], source_type: str = "text") -> DocInput:
    if not value.strip():
        raise ValidationFailed("source.value kosong", details={"field": "source.value"})
    ref = str(metadata.get("ref") or metadata.get("filename") or sha256_hex(value)[:32])
    title = str(metadata.get("title") or metadata.get("filename") or _title_from(value, ref))
    return DocInput(source_type, ref, title, value, metadata)


def report_markdown(report: AnalysisReport) -> str:
    parts = [f"# {report.title}", report.summary or ""]
    for section in (report.content or {}).get("sections", []):
        parts.append(f"## {section.get('title') or section.get('id')}")
        parts.append(str(section.get("markdown") or ""))
    return "\n\n".join(p for p in parts if p)


def _posts_docs(
    handle: str, rows: list[dict[str, Any]], metadata: dict[str, Any]
) -> list[DocInput]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("posted_at") or "")[:10] or "tanpa-tanggal"
        for kw in row.get("keywords") or ["-"]:
            groups[(day, str(kw))].append(row)
    docs = []
    for (day, kw), posts in sorted(groups.items()):
        posts.sort(key=lambda p: -int(p.get("likes") or 0))
        lines = [
            f"- @{p.get('author') or '?'} ({p.get('source') or '?'}, {int(p.get('likes') or 0)} "
            f"suka): {str(p.get('text') or '')[:POST_TEXT_CHARS]}"
            for p in posts[:MAX_POSTS_PER_DOC]
        ]
        title = f"Post '{kw}' {day}"
        body = f"# {title}\n\n{len(posts)} post.\n\n" + "\n".join(lines)
        docs.append(
            DocInput(
                "handle",
                f"{handle}:{day}:{kw}",
                title,
                body,
                {**metadata, "handle": handle, "day": day, "keyword": kw},
            )
        )
    return docs


async def resolve(
    services: Any,
    owner: Owner,
    source_type: SourceType,
    value: str,
    metadata: dict[str, Any],
    *,
    fetch_url: UrlFetcher | None = None,
) -> list[DocInput]:
    if source_type in ("text", "file_text"):
        return [text_doc(value, metadata, source_type)]
    if source_type == "url":
        if fetch_url is None:
            raise ValidationFailed("Ingest URL hanya berjalan di worker (butuh browser)")
        title, markdown = await fetch_url(value)
        if not markdown.strip():
            raise ValidationFailed(f"Halaman tanpa konten yang bisa diambil: {value[:200]}")
        return [DocInput("url", value, title or value, markdown, {**metadata, "url": value})]
    if source_type == "report":
        async with services.db.session() as session:
            report = await session.get(AnalysisReport, value)
        if report is None or not _report_visible(report, owner):
            raise ValidationFailed(f"Laporan {value} tidak ditemukan", details={"field": "source"})
        return [
            DocInput(
                "report",
                report.id,
                report.title,
                report_markdown(report),
                {**metadata, "report_kind": report.kind},
            )
        ]
    return await _handle_docs(services, owner, value, metadata)


def _report_visible(report: AnalysisReport, owner: Owner) -> bool:
    if owner.is_admin:
        return True
    if owner.client_id and report.client_id and str(report.client_id) == owner.client_id:
        return True
    return bool(owner.run_id and report.run_id and str(report.run_id) == owner.run_id)


async def _handle_docs(
    services: Any, owner: Owner, handle: str, metadata: dict[str, Any]
) -> list[DocInput]:
    async with services.db.session() as session:
        rs = await services.results.get_set(session, handle, owner)
        items = (
            await session.execute(
                select(ResultItem.ref_id, ResultItem.data)
                .where(ResultItem.handle == rs.handle)
                .order_by(ResultItem.idx)
                .limit(10_000)
            )
        ).all()
        if rs.kind == "web_pages":
            ids = [r.ref_id for r in items if r.ref_id][:MAX_HANDLE_DOCS]
            pages = (
                await session.scalars(
                    select(WebPage).where(WebPage.id.in_(ids), WebPage.content_md.is_not(None))
                )
            ).all()
            return [
                DocInput(
                    "url",
                    p.final_url or p.url,
                    p.title or p.url,
                    p.content_md or "",
                    {**metadata, "url": p.final_url or p.url, "handle": handle, **_published(p)},
                )
                for p in pages
                if (p.content_md or "").strip()
            ]
    if rs.kind == "social_posts":
        return _posts_docs(handle, [dict(r.data) for r in items], metadata)
    raise ValidationFailed(
        f"Handle jenis '{rs.kind}' belum didukung untuk ingest (web_pages / social_posts)"
    )


def _published(page: WebPage) -> dict[str, Any]:
    published: datetime | None = page.published_at
    return {"published_at": published.date().isoformat()} if published else {}


def collection_name_for_project(project_id: uuid.UUID | str) -> str:
    return f"dashboard:{project_id}"
