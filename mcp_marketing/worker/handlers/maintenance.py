"""Job pemeliharaan terjadwal: cache_cleanup, retention, usage_rollup (DATABASE §6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.utils.time import now
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

ZERO_UUID = "00000000-0000-0000-0000-000000000000"

ROLLUP_SQL = f"""
INSERT INTO mcp.usage_daily AS u (day, client_id, tool, calls, errors, cache_hits, output_tokens,
                                  baseline_tokens, llm_tokens, cost_usd, external_units)
SELECT day, client_id, tool, sum(calls), sum(errors), sum(cache_hits), sum(output_tokens),
       sum(baseline_tokens), sum(llm_tokens), sum(cost_usd), 0
FROM (
  SELECT (created_at AT TIME ZONE :tz)::date AS day,
         COALESCE(client_id, '{ZERO_UUID}'::uuid) AS client_id, tool,
         count(*) AS calls, count(*) FILTER (WHERE status = 'error') AS errors,
         count(*) FILTER (WHERE status = 'cached') AS cache_hits,
         sum(output_tokens) AS output_tokens, sum(COALESCE(baseline_tokens, 0)) AS baseline_tokens,
         sum(llm_tokens) AS llm_tokens, 0::numeric AS cost_usd
  FROM mcp.tool_calls WHERE created_at >= :since GROUP BY 1, 2, 3
  UNION ALL
  -- token LLM di luar tool (loop agent, kompaksi riwayat) per purpose
  SELECT (created_at AT TIME ZONE :tz)::date, COALESCE(client_id, '{ZERO_UUID}'::uuid),
         left('llm:' || split_part(purpose, '@', 1), 64),
         count(*), count(*) FILTER (WHERE status = 'error'), count(*) FILTER (WHERE cached),
         0, 0, sum(prompt_tokens + completion_tokens), COALESCE(sum(cost_usd), 0)
  FROM mcp.llm_calls WHERE created_at >= :since GROUP BY 1, 2, 3
) t
GROUP BY day, client_id, tool
ON CONFLICT (day, client_id, tool) DO UPDATE SET
  calls = EXCLUDED.calls, errors = EXCLUDED.errors, cache_hits = EXCLUDED.cache_hits,
  output_tokens = EXCLUDED.output_tokens, baseline_tokens = EXCLUDED.baseline_tokens,
  llm_tokens = EXCLUDED.llm_tokens, cost_usd = EXCLUDED.cost_usd
"""


class CacheCleanupHandler:
    kind = "cache_cleanup"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=5)

    async def run(self, job: JobCtx) -> JobResult:
        async with job.services.db.session() as session:
            cache = (
                await session.execute(
                    text("DELETE FROM mcp.cache_entries WHERE expires_at <= now()")
                )
            ).rowcount  # type: ignore[attr-defined]
            handles = (
                await session.execute(text("DELETE FROM mcp.result_sets WHERE expires_at <= now()"))
            ).rowcount  # type: ignore[attr-defined]
        return JobResult(
            {
                "summary": f"{cache} cache & {handles} handle kedaluwarsa dihapus",
                "stats": {"cache_entries": cache, "result_sets": handles},
            }
        )


class RetentionHandler:
    kind = "retention"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=60)

    async def run(self, job: JobCtx) -> JobResult:
        w = job.services.settings.worker
        tz = job.services.settings.timezone
        stats: dict[str, int] = {}
        async with job.services.db.session() as session:
            # rollup dulu agar audit yang dihapus sudah tercatat di usage_daily
            await session.execute(
                text(ROLLUP_SQL), {"tz": tz, "since": _days_ago(w.audit_retention_days + 2)}
            )
            statements = {
                "llm_calls": "DELETE FROM mcp.llm_calls WHERE created_at < "
                "now() - make_interval(days => :d)",
                "tool_calls": "DELETE FROM mcp.tool_calls WHERE created_at < "
                "now() - make_interval(days => :d)",
            }
            for name, sql in statements.items():
                stats[name] = (
                    await session.execute(text(sql), {"d": w.audit_retention_days})
                ).rowcount  # type: ignore[attr-defined]
            stats["webhook_outbox"] = (
                await session.execute(
                    text(
                        "DELETE FROM mcp.webhook_outbox "
                        "WHERE status IN ('delivered','duplicate','discarded') "
                        "AND created_at < now() - interval '30 days'"
                    )
                )
            ).rowcount  # type: ignore[attr-defined]
            stats["run_steps"] = (
                await session.execute(
                    text("DELETE FROM mcp.run_steps WHERE created_at < now() - interval '90 days'")
                )
            ).rowcount  # type: ignore[attr-defined]
            stats["social_raw"] = (
                await session.execute(
                    text(
                        "UPDATE mcp.social_posts SET raw = NULL WHERE raw IS NOT NULL "
                        "AND collected_at < now() - make_interval(days => :d)"
                    ),
                    {"d": w.raw_retention_days},
                )
            ).rowcount  # type: ignore[attr-defined]
            # DATABASE §6: belum ada laporan/KB yang merujuk post/halaman, jadi dihapus penuh
            for table, column in (("social_posts", "collected_at"), ("web_pages", "fetched_at")):
                stats[table] = (
                    await session.execute(
                        text(
                            f"DELETE FROM mcp.{table} "
                            f"WHERE {column} < now() - make_interval(days => :d)"
                        ),
                        {"d": w.social_retention_days},
                    )
                ).rowcount  # type: ignore[attr-defined]
            stats["result_sets"] = (
                await session.execute(text("DELETE FROM mcp.result_sets WHERE expires_at <= now()"))
            ).rowcount  # type: ignore[attr-defined]
        return JobResult({"summary": "retensi selesai", "stats": stats})


class UsageRollupHandler:
    kind = "usage_rollup"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=5)

    async def run(self, job: JobCtx) -> JobResult:
        async with job.services.db.session() as session:
            await session.execute(
                text(ROLLUP_SQL),
                {
                    "tz": job.services.settings.timezone,
                    "since": _days_ago(int(job.params.get("days", 2))),
                },
            )
        return JobResult({"summary": "rollup usage_daily selesai"})


def _days_ago(days: int) -> datetime:
    return now() - timedelta(days=days)
