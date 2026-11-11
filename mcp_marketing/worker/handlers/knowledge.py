"""Job `kn_asset_ingest` (MCP-V1-11): ekstraksi PDF -> chunk -> embed-local -> `books:<ws>`."""

from __future__ import annotations

import uuid
from typing import Any

from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.knowledge.ingest import ingest_asset
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

# perkiraan kasar: ekstraksi ±0,05 s/halaman + embed-local ±1,5 s per batch 8 chunk (LGW-V1-05)
SECONDS_PER_PAGE = 0.6


class KnAssetIngestHandler:
    kind = "kn_asset_ingest"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        pages = int(params.get("pages") or 300)
        return JobEstimate(seconds=max(30, int(pages * SECONDS_PER_PAGE)))

    async def run(self, job: JobCtx) -> JobResult:
        stats = await ingest_asset(
            job.services,
            uuid.UUID(job.params["asset_id"]),
            progress=job.progress,
        )
        summary = (
            f"{stats['pages']} halaman, {stats['chunks']} chunk"
            + (" (isi sama, tidak di-embed ulang)" if stats.get("skipped") else "")
            + (
                f"; {stats['pages_needs_ocr']} halaman perlu OCR"
                if stats["pages_needs_ocr"]
                else ""
            )
        )
        return JobResult({"summary": summary, "asset_id": job.params["asset_id"], **stats})
