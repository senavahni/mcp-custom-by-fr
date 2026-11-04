"""Sintesis laporan job `intel`: satu panggilan LLM JSON memakai route agent tahap Analisis."""

from __future__ import annotations

from typing import Any

import orjson
from pydantic import BaseModel, Field, field_validator

from mcp_marketing.schemas.intel import RECOMMENDATIONS_MAX, SUMMARY_MD_MAX_CHARS, TOP_MAX, JobSpec
from mcp_marketing.services.agent.budget import BudgetScope
from mcp_marketing.services.llm import prompts

PROMPT = "intel_synthesis"
PROMPT_VERSION = "v1"
SAMPLE_TEXT_CHARS = 280
TOPIC_LABEL_CHARS = 80
RECOMMENDATION_CHARS = 300


class SynthTopic(BaseModel):
    label: str
    share: float = 0.0

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return v.strip()[:TOPIC_LABEL_CHARS]

    @field_validator("share")
    @classmethod
    def _share(cls, v: float) -> float:
        return min(1.0, max(0.0, float(v)))


class SynthesisOut(BaseModel):
    """Toleran terhadap output LLM yang kelebihan panjang: dipotong, bukan ditolak."""

    summary_md: str = ""
    topics: list[SynthTopic] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)

    @field_validator("summary_md")
    @classmethod
    def _summary(cls, v: str) -> str:
        return v.strip()[:SUMMARY_MD_MAX_CHARS]

    @field_validator("topics")
    @classmethod
    def _topics(cls, v: list[SynthTopic]) -> list[SynthTopic]:
        return [t for t in v if t.label][:TOP_MAX]

    @field_validator("recommendations")
    @classmethod
    def _recs(cls, v: list[str]) -> list[str]:
        return [r.strip()[:RECOMMENDATION_CHARS] for r in v if r.strip()][:RECOMMENDATIONS_MAX]


def render_samples(sample: list[dict[str, Any]]) -> str:
    lines = []
    for i, r in enumerate(sample, 1):
        text = " ".join(str(r.get("text") or "").split())[:SAMPLE_TEXT_CHARS]
        lines.append(
            f"{i}. [{r.get('source')}|{r.get('label') or '-'}|eng {r['engagement']}] {text}"
        )
    return "\n".join(lines)


def build_prompt(spec: JobSpec, stats: dict[str, Any], sample: list[dict[str, Any]]) -> str:
    return prompts.render(
        PROMPT,
        PROMPT_VERSION,
        language="Indonesia" if spec.language == "id" else "English",
        title=spec.title,
        period=f"{spec.period.since.isoformat()} s/d {spec.period.until.isoformat()}",
        sources=", ".join(spec.source_types()),
        aspects=", ".join(spec.stages.analyze.aspects) or "-",
        stats=orjson.dumps(stats, option=orjson.OPT_INDENT_2).decode(),
        sample_count=len(sample),
        samples=render_samples(sample),
    )


async def synthesize(
    services: Any,
    *,
    spec: JobSpec,
    route: str,
    stats: dict[str, Any],
    sample: list[dict[str, Any]],
    budget: BudgetScope | None,
) -> tuple[SynthesisOut, int]:
    """(hasil, token terpakai). Error LLM diteruskan ke pemanggil untuk dipetakan."""
    assert services.llm is not None
    out, result = await services.llm.chat_json(
        "report",
        [{"role": "user", "content": build_prompt(spec, stats, sample)}],
        SynthesisOut,
        budget=budget,
        prompt_version=PROMPT_VERSION,
        route_override=route,
    )
    return out, result.prompt_tokens + result.completion_tokens
