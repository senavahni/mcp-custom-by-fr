"""Job Google Trends: `trends_interest`, `trends_region`, `trends_related` (TASK-17)."""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.collectors.trends_service import (
    TrendsService,
    interest_output,
    previous_period,
    region_output,
    related_output,
)
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.worker.handlers.base import JobCtx, JobResult


def _dates(params: dict[str, Any]) -> tuple[date, date]:
    return date.fromisoformat(params["start"]), date.fromisoformat(params["end"])


class TrendsInterestHandler:
    kind = "trends_interest"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        groups = max(1, (len(params.get("keywords") or []) + 3) // 4)
        return JobEstimate(seconds=20 * groups + 15)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        start, end = _dates(p)
        async with TrendsService(job.services, job_id=job.job_id) as svc:
            record = await svc.interest(
                p["keywords"],
                start,
                end,
                p["geo"],
                int(p.get("category") or 0),
                p.get("gprop", "web"),
                anchor=p.get("anchor"),
            )
            prev = None
            if p.get("compare_previous", True) and len(p["keywords"]) <= 5:
                ps, pe = previous_period(start, end)
                prev = await svc.interest(
                    p["keywords"],
                    ps,
                    pe,
                    p["geo"],
                    int(p.get("category") or 0),
                    p.get("gprop", "web"),
                )
            extra = 0 if prev is None or prev.cache_hit else 1
        output, handle = await interest_output(
            job.services,
            Owner(client_id=job.client_id, run_id=job.run_id),
            record,
            prev,
            extra_requests=extra,
        )
        return JobResult(result=output, result_handle=handle)


class TrendsRegionHandler:
    kind = "trends_region"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=20)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        start, end = _dates(p)
        async with TrendsService(job.services, job_id=job.job_id) as svc:
            record = await svc.region(
                p["keyword"], start, end, p["geo"], p.get("resolution", "REGION")
            )
        return JobResult(result=region_output(record, p["keyword"], int(p.get("top_n") or 10)))


class TrendsRelatedHandler:
    kind = "trends_related"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=20)

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        start, end = _dates(p)
        async with TrendsService(job.services, job_id=job.job_id) as svc:
            record = await svc.related(p["keyword"], start, end, p["geo"], p.get("kind", "queries"))
        return JobResult(result=related_output(record, p["keyword"], p.get("kind", "queries")))
