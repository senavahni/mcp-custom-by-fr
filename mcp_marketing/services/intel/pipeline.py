"""Job induk `intel_pipeline`: Kumpulkan (job anak per sumber) -> Analisis (sentimen + sintesis).

Agent tiap tahap menentukan route LLM, budget token, dan atribusi (`X-Caller-Agent`,
`mcp.llm_calls.agent_key`). Setiap transisi dicatat di `mcp.intel_jobs` + event `intel.job.*`.
Job induk dibuat dengan `max_attempts=1`: kegagalan = job gagal (Dashboard mengulang = job baru).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_marketing.core import context
from mcp_marketing.core.errors import SourceUnavailable, ToolError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import AgentProfile
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.schemas.intel import JobResultView, JobSpec
from mcp_marketing.services.agent.budget import BudgetLayer, BudgetScope
from mcp_marketing.services.intel import analysis, kb_index
from mcp_marketing.services.intel.capabilities import validate_stage_agents
from mcp_marketing.services.intel.children import run_child
from mcp_marketing.services.intel.errors import map_error
from mcp_marketing.services.intel.estimate import route_tier, usd_for_tokens
from mcp_marketing.services.intel.sources import PreparedSource, prepare_source
from mcp_marketing.services.intel.synthesize import synthesize
from mcp_marketing.services.intel.tracker import IntelTracker
from mcp_marketing.services.kb.embedder import embedder_for
from mcp_marketing.services.nlp.sentiment import SentimentAnalyzer
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.worker.handlers.base import Handler, JobCancelled, JobCtx, JobResult
from mcp_marketing.worker.handlers.social import ThreadsSearchHandler, XSearchHandler
from mcp_marketing.worker.handlers.trends import TrendsInterestHandler
from mcp_marketing.worker.handlers.web import WebCrawlHandler, WebScrapeHandler

log = get_logger(__name__)

KIND = "intel_pipeline"
CHILD_HANDLERS: dict[str, Handler] = {
    h.kind: h
    for h in (
        XSearchHandler(),
        ThreadsSearchHandler(),
        WebScrapeHandler(),
        WebCrawlHandler(),
        TrendsInterestHandler(),
    )
}
GOOGLE_TRENDS_KEYS = ("summary", "stats", "table", "items")


@dataclass
class Collected:
    items: list[dict[str, Any]]
    google_trends: dict[str, Any] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    external_units: int = 0
    usd_external: float = 0.0


@dataclass
class CostTracker:
    external_units: int = 0
    usd_external: float = 0.0
    by_stage: list[dict[str, Any]] = field(default_factory=list)

    def add_llm(self, stage: str, agent_key: str, tokens: int, usd: float) -> None:
        self.by_stage.append(
            {"stage": stage, "agent_key": agent_key, "llm_tokens": tokens, "usd_llm": round(usd, 4)}
        )

    def view(self) -> dict[str, Any]:
        return {
            "llm_tokens": sum(s["llm_tokens"] for s in self.by_stage),
            "usd_llm": round(sum(s["usd_llm"] for s in self.by_stage), 4),
            "external_units": self.external_units,
            "usd_external": round(self.usd_external, 4),
            "by_stage": self.by_stage,
        }


def stage_budget(profile: AgentProfile) -> BudgetScope:
    return BudgetScope(layers=[BudgetLayer(f"intel:{profile.agent_key}", profile.run_token_budget)])


def _google_trends(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in GOOGLE_TRENDS_KEYS if k in result}


class IntelPipelineHandler:
    kind = KIND

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(**(params.get("estimate") or {}))

    async def run(self, job: JobCtx) -> JobResult:
        tracker = IntelTracker(job.services, job.job_id)
        intel = await tracker.load()
        spec = JobSpec.model_validate(intel.spec)
        cost = CostTracker()
        stage: str | None = None
        try:
            async with job.services.db.session() as session:
                profiles = await validate_stage_agents(session, spec)
            await tracker.emit("intel.job.started", {})
            stage = "collect"
            collected = await self._collect(job, tracker, spec, profiles["collect"], cost)
            stage = "analyze"
            final, rows, handle = await self._analyze(
                job, tracker, spec, profiles["analyze"], collected, cost
            )
            if "index" in profiles:
                stage = "index"
                await self._index(job, tracker, spec, profiles["index"], intel, final, rows, cost)
            await tracker.completed(final, cost.view())
            total = len(rows)
            summary = f"{total} item dianalisis" if total else "0 item"
            return JobResult(
                result={
                    "summary": summary,
                    "total": total,
                    "cost": {"llm_tokens": cost.view()["llm_tokens"], "external_units": 0},
                },
                result_handle=handle,
            )
        except JobCancelled:
            await tracker.cancelled()
            raise
        except ToolError as exc:
            await tracker.failed(stage, map_error(exc, stage), cost=cost.view())
            raise
        except Exception:
            log.exception("intel.pipeline_internal_error", job_id=job.job_id, stage=stage)
            error = {
                "code": "INTERNAL",
                "message": "Kesalahan internal pipeline",
                "retryable": True,
            }
            await tracker.failed(stage, error, cost=cost.view())
            raise

    # ------------------------------------------------------------------ Kumpulkan
    async def _collect(
        self,
        job: JobCtx,
        tracker: IntelTracker,
        spec: JobSpec,
        profile: AgentProfile,
        cost: CostTracker,
    ) -> Collected:
        await tracker.stage_started("collect", profile.agent_key)
        collected = Collected(items=[])
        done: list[tuple[PreparedSource, JobResult]] = []
        with context.context_scope(agent_key=profile.agent_key, stage="collect"):
            prepared = [
                await prepare_source(job.services, src, spec.period) for src in spec.sources
            ]
            for p in prepared:
                await job.raise_if_cancelled()
                try:
                    res = await run_child(
                        job,
                        CHILD_HANDLERS[p.kind],
                        p.params,
                        timeout_s=p.timeout_s,
                        on_progress=self._progress_cb(tracker, p.type),
                    )
                except ToolError as exc:
                    if len(prepared) == 1:
                        raise
                    message = f"{p.type}: {exc.message}"[:300]
                    collected.warnings.append({"code": "SOURCE_FAILED", "message": message})
                    continue
                done.append((p, res))
                child_cost = res.result.get("cost") or {}
                stats_cost = (res.result.get("stats") or {}).get("cost") or {}
                cost.external_units += int(child_cost.get("external_units") or 0)
                cost.usd_external += float(stats_cost.get("usd") or 0)
        if not done:
            raise SourceUnavailable(
                "Semua sumber gagal dikumpulkan", details={"warnings": collected.warnings}
            )
        owner = Owner(client_id=job.client_id)
        seen: set[str] = set()
        for p, res in done:
            if p.kind == "trends_interest":
                collected.google_trends = _google_trends(res.result)
                continue
            if not res.result_handle:
                continue
            for item in await analysis.load_items(job.services, owner, p.kind, res.result_handle):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    collected.items.append(item)
        totals = analysis.totals(collected.items)
        await tracker.stage_completed(
            "collect", stats={"total": totals["items"], "by_source": totals["by_source"]},
            warnings=collected.warnings,
        )  # fmt: skip
        return collected

    @staticmethod
    def _progress_cb(tracker: IntelTracker, source: str) -> Any:
        async def on_progress(done: int, total: int | None, message: str | None) -> None:
            await tracker.progress("collect", done, total, f"{source}: {message or ''}".strip())

        return on_progress

    # ------------------------------------------------------------------ Analisis
    async def _analyze(
        self,
        job: JobCtx,
        tracker: IntelTracker,
        spec: JobSpec,
        profile: AgentProfile,
        collected: Collected,
        cost: CostTracker,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        """(hasil final tervalidasi, baris item berlabel, handle item) — belum `completed`."""
        tz = job.services.settings.timezone
        warnings = list(collected.warnings)
        if not collected.items:
            warnings.append({"code": "NO_ITEMS", "message": "Tidak ada data teks pada periode ini"})
            await tracker.stage_completed("analyze", skipped=True, warnings=warnings)
            result = self._validated(self._result([], None, collected, warnings, tz, handle=None))
            return result, [], None

        await tracker.stage_started("analyze", profile.agent_key)
        cfg = spec.stages.analyze
        budget = stage_budget(profile)
        total = len(collected.items)
        with context.context_scope(agent_key=profile.agent_key, stage="analyze"):
            await tracker.progress("analyze", 0, total, "klasifikasi sentimen")
            analyzer = SentimentAnalyzer(
                job.services,
                budget=budget,
                run_cpu=job.run_cpu if job.executor is not None else None,
            )
            outcome = await analyzer.analyze(
                analysis.to_sentiment_items(collected.items),
                method=cfg.method,
                aspects=list(cfg.aspects) or None,
            )
            sentiment_tokens = int(outcome.cost["llm_tokens"])
            rows = analysis.label_rows(collected.items, outcome)
            sentiment = analysis.sentiment_summary(rows, tz)
            rs = await job.services.results.create(
                kind="intel_items",
                columns=analysis.ITEM_COLS,
                rows=rows,
                owner=Owner(client_id=job.client_id),
                job_id=job.job_id,
                sort_ts="posted_at",
                sort_num="engagement",
                stats=analysis.totals(rows),
                source_ref={"tool": "intel", "job_id": job.job_id},
            )
            result = self._result(rows, sentiment, collected, warnings, tz, handle=rs.handle)
            result["items_expire_at"] = rs.expires_at.isoformat()
            await tracker.progress("analyze", total, total, "menyusun ringkasan")
            synth_tokens = 0
            try:
                synth, synth_tokens = await synthesize(
                    job.services,
                    spec=spec,
                    route=profile.route,
                    stats=analysis.stats_for_prompt(rows, sentiment),
                    sample=analysis.stratified_sample(rows),
                    budget=budget,
                )
                result["summary_md"] = synth.summary_md or None
                result["recommendations"] = synth.recommendations
                result["trends"]["topics"] = [
                    {"label": t.label, "count": round(t.share * total), "share": round(t.share, 4)}
                    for t in synth.topics
                ]
            except ToolError as exc:
                if (exc.details or {}).get("reason") != "invalid_json":
                    self._add_llm_cost(cost, profile, sentiment_tokens, 0, job)
                    partial = self._validated({**result, "partial": True})
                    await tracker.failed(
                        "analyze", map_error(exc, "analyze"), result=partial, cost=cost.view()
                    )
                    raise
                warnings.append(
                    {"code": "SUMMARY_UNAVAILABLE", "message": "Ringkasan naratif tidak tersedia"}
                )
        self._add_llm_cost(cost, profile, sentiment_tokens, synth_tokens, job)
        final = self._validated(result)
        await tracker.stage_completed(
            "analyze", stats={"labeled": total}, warnings=warnings[len(collected.warnings) :]
        )
        return final, rows, rs.handle

    # ------------------------------------------------------------------ Indeks (KB)
    async def _index(
        self,
        job: JobCtx,
        tracker: IntelTracker,
        spec: JobSpec,
        profile: AgentProfile,
        intel: Any,
        final: dict[str, Any],
        rows: list[dict[str, Any]],
        cost: CostTracker,
    ) -> None:
        """Simpan ke KB. Gagal = tidak fatal: tahap `skipped` + peringatan INDEX_FAILED."""
        docs = kb_index.build_docs(job.job_id, spec.title, rows, final)
        if not docs:
            warning = {"code": "NO_ITEMS", "message": "Tidak ada konten untuk Knowledge Base"}
            await tracker.stage_completed("index", skipped=True, warnings=[warning])
            return
        await tracker.stage_started("index", profile.agent_key)

        async def on_progress(done: int, total: int, tokens: list[str]) -> None:
            await tracker.progress(
                "index", done, total, "embedding ke Knowledge Base", sample_tokens=tokens
            )

        try:
            with context.context_scope(agent_key=profile.agent_key, stage="index"):
                coll = await kb_index.intel_collection(job.services, intel.workspace_id)
                embedder = embedder_for(
                    job.services, coll.embed_model, coll.embed_dim, stage_budget(profile)
                )
                stats = await kb_index.index_documents(
                    job.services.db,
                    coll,
                    docs,
                    embedder,
                    progress=on_progress,
                    checkpoint=job.raise_if_cancelled,
                )
        except ToolError as exc:
            warning = {"code": "INDEX_FAILED", "message": f"Gagal menyimpan ke KB: {exc.message}"}
            final["warnings"].append({k: v[:300] for k, v in warning.items()})
            await tracker.stage_completed("index", skipped=True, warnings=[final["warnings"][-1]])
            return
        cost.add_llm(
            "index", profile.agent_key, stats.tokens, usd_for_tokens(stats.tokens, "embed")
        )
        await tracker.stage_completed("index", stats=stats.view(coll.name))

    @staticmethod
    def _add_llm_cost(
        cost: CostTracker, profile: AgentProfile, sentiment: int, synth: int, job: JobCtx
    ) -> None:
        tier = route_tier(profile.route, job.services.settings.llm)
        usd = usd_for_tokens(sentiment, "fast") + usd_for_tokens(synth, tier)
        cost.add_llm("analyze", profile.agent_key, sentiment + synth, usd)

    @staticmethod
    def _result(
        rows: list[dict[str, Any]],
        sentiment: dict[str, Any] | None,
        collected: Collected,
        warnings: list[dict[str, str]],
        tz: str,
        *,
        handle: str | None,
    ) -> dict[str, Any]:
        return {
            "totals": analysis.totals(rows),
            "sentiment": sentiment,
            "trends": {
                "topics": [],
                "hashtags": analysis.top_pairs([h for r in rows for h in r.get("hashtags") or []]),
                "authors": analysis.top_pairs([r["author"] for r in rows if r.get("author")]),
            },
            "volume_by_day": analysis.volume_by_day(rows, tz),
            "google_trends": collected.google_trends,
            "summary_md": None,
            "recommendations": [],
            "items_handle": handle,
            "items_expire_at": None,
            "partial": False,
            "warnings": warnings,
        }

    @staticmethod
    def _validated(result: dict[str, Any]) -> dict[str, Any]:
        """Hasil selalu lolos kontrak sebelum disimpan/dikirim (gagal = bug, tertangkap test)."""
        return JobResultView.model_validate(result).model_dump(mode="json")
