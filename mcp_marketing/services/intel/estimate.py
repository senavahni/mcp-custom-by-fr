"""Estimasi durasi & biaya job `intel` sebelum dijalankan (kontrak 1.2 §2.3).

Biaya LLM adalah PERKIRAAN: token per item × harga campuran per tier route (profil Gateway
`remote-only`: fast = gpt-5-mini, smart = gpt-5). Biaya aktual dihitung dari token nyata.
"""

from __future__ import annotations

from typing import Any

from mcp_marketing.models import AgentProfile
from mcp_marketing.schemas.intel import (
    Estimate,
    EstimateLlm,
    EstimateSource,
    EstimateStage,
    IntelWarning,
    JobSpec,
)
from mcp_marketing.services.intel.sources import PreparedSource, prepare_source
from mcp_marketing.services.nlp.sentiment_model import model_available

# USD per 1.000 token (campuran input/output) per tier route; embed = text-embedding-3-small
USD_PER_1K_TOKENS = {"fast": 0.0006, "smart": 0.005, "embed": 0.00002}
INDEX_TOKENS_PER_ITEM = 80
INDEX_SUMMARY_TOKENS = 600
INDEX_ITEMS_PER_SECOND = 50
INDEX_BASE_SECONDS = 10
SENTIMENT_TOKENS_PER_ITEM = 90
SENTIMENT_BATCH = 50
SENTIMENT_BATCH_OVERHEAD = 300
ASPECT_FACTOR = 0.6
# porsi item yang tetap dikirim ke LLM bila model sentimen lokal tersedia (confidence rendah)
LOCAL_MODEL_LLM_SHARE = 0.25
SYNTHESIS_TOKENS = 7_000
ANALYSIS_ITEMS_PER_SECOND = 30
ANALYSIS_BASE_SECONDS = 30
CONFIRM_USD_THRESHOLD = 1.0


def route_tier(route: str, llm_settings: Any) -> str:
    """Tier harga dari route profil agent (`fast`/`smart` atau alias route Gateway)."""
    if route in ("fast", llm_settings.llm_route_fast):
        return "fast"
    return "smart"


def usd_for_tokens(tokens: int, tier: str) -> float:
    return round(tokens / 1_000 * USD_PER_1K_TOKENS[tier], 4)


def sentiment_tokens(items: int, *, method: str, has_model: bool, aspects: int) -> int:
    if items <= 0 or method == "model":
        return 0
    llm_items = items if (method == "llm" or not has_model) else int(items * LOCAL_MODEL_LLM_SHARE)
    batches = -(-llm_items // SENTIMENT_BATCH)
    tokens = llm_items * SENTIMENT_TOKENS_PER_ITEM + batches * SENTIMENT_BATCH_OVERHEAD
    if aspects:
        tokens += int(tokens * ASPECT_FACTOR)
    return tokens


def analysis_llm(
    services: Any, spec: JobSpec, analyze_profile: AgentProfile, items: int
) -> tuple[int, float, EstimateStage]:
    """(token, usd, rincian tahap analisis) — sentimen di tier fast, sintesis di route agent."""
    llm = services.settings.llm
    has_model = model_available(services.settings.nlp.sentiment_model_dir)
    analyze = spec.stages.analyze
    sent = sentiment_tokens(
        items, method=analyze.method, has_model=has_model, aspects=len(analyze.aspects)
    )
    synth = SYNTHESIS_TOKENS if items > 0 else 0
    tier = route_tier(analyze_profile.route, llm)
    usd = usd_for_tokens(sent, "fast") + usd_for_tokens(synth, tier)
    stage = EstimateStage(
        stage="analyze",
        agent_key=analyze_profile.agent_key,
        route=analyze_profile.route,
        tokens_est=sent + synth,
    )
    return sent + synth, round(usd, 4), stage


def index_llm(
    services: Any, index_profile: AgentProfile, items: int
) -> tuple[int, float, EstimateStage]:
    """(token, usd, rincian tahap index) — embedding per item + dokumen ringkasan."""
    tokens = items * INDEX_TOKENS_PER_ITEM + INDEX_SUMMARY_TOKENS if items > 0 else 0
    stage = EstimateStage(
        stage="index",
        agent_key=index_profile.agent_key,
        route=services.settings.llm.llm_route_embed,
        tokens_est=tokens,
    )
    return tokens, usd_for_tokens(tokens, "embed"), stage


async def build_estimate(
    services: Any, spec: JobSpec, profiles: dict[str, AgentProfile]
) -> tuple[Estimate, list[PreparedSource]]:
    prepared = [await prepare_source(services, src, spec.period) for src in spec.sources]
    items = sum(p.expected_items for p in prepared)
    tokens, usd_llm, stage = analysis_llm(services, spec, profiles["analyze"], items)
    stages = [stage]
    if "index" in profiles:
        idx_tokens, idx_usd, idx_stage = index_llm(services, profiles["index"], items)
        tokens, usd_llm = tokens + idx_tokens, round(usd_llm + idx_usd, 4)
        stages.append(idx_stage)
    usd_external = round(sum(p.usd_external for p in prepared), 4)
    warnings = [IntelWarning(**w) for p in prepared for w in p.warnings]
    if items > 0 and not model_available(services.settings.nlp.sentiment_model_dir):
        warnings.append(
            IntelWarning(
                code="SENTIMENT_MODEL_MISSING",
                message="Model sentimen lokal belum ada; seluruh klasifikasi memakai LLM",
            )
        )
    seconds = sum(p.seconds for p in prepared)
    if items:
        seconds += ANALYSIS_BASE_SECONDS + items // ANALYSIS_ITEMS_PER_SECOND
        if "index" in profiles:
            seconds += INDEX_BASE_SECONDS + items // INDEX_ITEMS_PER_SECOND
    estimate = Estimate(
        seconds=seconds,
        sources=[
            EstimateSource(
                type=p.type,  # type: ignore[arg-type]
                mode=p.mode,  # type: ignore[arg-type]
                external_units=p.external_units,
                usd_external=p.usd_external,
            )
            for p in prepared
        ],
        llm=EstimateLlm(tokens_est=tokens, usd_est=usd_llm, by_stage=stages),
        requires_confirmation=usd_external > 0 or usd_llm > CONFIRM_USD_THRESHOLD,
        warnings=warnings,
    )
    return estimate, prepared
