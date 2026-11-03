"""Job KB (TASK-19): `kb_ingest` dan `kb_sync_dashboard`."""

from __future__ import annotations

import uuid
from typing import Any

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.browser.driver_pool import launcher_for
from mcp_marketing.services.kb import ingest
from mcp_marketing.services.kb.embedder import embedder_for
from mcp_marketing.services.kb.store import get_collection, upsert_document
from mcp_marketing.services.kb.sync import sync_project
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.worker.handlers.base import JobCtx, JobResult
from mcp_marketing.worker.handlers.web import PageScraper, ScrapeOptions

URL_MAX_CHARS = 100_000


def _url_fetcher(job: JobCtx) -> ingest.UrlFetcher:
    async def fetch(url: str) -> tuple[str | None, str]:
        scraper = PageScraper(job, ScrapeOptions(mode="markdown", max_chars=URL_MAX_CHARS))
        async with launcher_for(job.services).session("default", job.job_id) as page:
            record, extracted, _ = await scraper.scrape(page, url)
        if record["status"] != "ok" or extracted is None:
            raise ValidationFailed(f"URL tidak dapat diambil (status {record['status']})")
        return extracted.title, extracted.content

    return fetch


class KbIngestHandler:
    kind = "kb_ingest"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        source = params.get("source") or {}
        heavy = source.get("type") in ("url", "handle")
        return JobEstimate(seconds=60 if heavy else 15)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        coll = await get_collection(job.services.db, p["collection_id"])
        embedder = embedder_for(job.services, coll.embed_model, coll.embed_dim)
        source = p["source"]
        docs = await ingest.resolve(
            job.services,
            Owner(client_id=job.client_id, run_id=job.run_id),
            source["type"],
            source["value"],
            dict(p.get("metadata") or {}),
            fetch_url=_url_fetcher(job) if source["type"] == "url" else None,
        )
        stats = {"documents": len(docs), "created": 0, "updated": 0, "skipped": 0}
        stats |= {"chunks": 0, "tokens_embedded": 0}
        for i, doc in enumerate(docs, 1):
            await job.raise_if_cancelled()
            res = await upsert_document(
                job.services.db,
                coll,
                doc,
                embedder,
                chunk_size=int(p.get("chunk_size") or 800),
                overlap=int(p.get("overlap") or 100),
            )
            stats[res.status] += 1
            stats["chunks"] += res.chunks
            stats["tokens_embedded"] += res.tokens_embedded
            await job.progress(i, len(docs), f"{i}/{len(docs)} dokumen")
        summary = (
            f"{stats['documents']} dokumen ({stats['created']} baru, {stats['updated']} berubah, "
            f"{stats['skipped']} sama), {stats['chunks']} chunk di-embed"
        )
        return JobResult({"summary": summary, "collection_id": str(coll.id), **stats})


class KbSyncDashboardHandler:
    kind = "kb_sync_dashboard"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=30)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        ws = p.get("workspace_id")
        stats = await sync_project(
            job.services,
            uuid.UUID(p["project_id"]),
            lambda coll: embedder_for(job.services, coll.embed_model, coll.embed_dim),
            workspace_id=uuid.UUID(ws) if ws else None,
            full=bool(p.get("full")),
            progress=lambda done, total: job.progress(done, total, f"{done}/{total} dokumen"),
        )
        summary = (
            f"Sinkron {stats['documents']} dokumen: {stats['created']} baru, "
            f"{stats['updated']} berubah, {stats['skipped']} sama, {stats['deleted']} dihapus"
        )
        return JobResult({"summary": summary, **stats})
