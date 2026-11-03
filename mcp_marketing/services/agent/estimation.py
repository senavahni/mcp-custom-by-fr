"""Estimasi durasi task (pm_request_agent_estimation, kontrak §3.2): heuristik + 1 LLM `fast`.

Hasil LLM dibatasi 0,5×–2× heuristik; kalender hanya hari kerja Senin–Jumat mulai
`earliest_start_date` (zona input). LLM gagal/lambat -> hasil heuristik confidence ≤ 0,4.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from mcp_marketing.core.errors import ToolError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.schemas.contract import EstimationInput, EstimationOutput
from mcp_marketing.services.agent.budget import BudgetScope
from mcp_marketing.services.llm import prompts
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import add_workdays, next_workday

log = get_logger(__name__)

LLM_TIMEOUT_S = 20.0
FALLBACK_CONFIDENCE = 0.4
DESCRIPTION_CHARS = 1_500
MIN_DAYS = 0.5
MAX_DAYS = 60.0
_RESEARCH = re.compile(
    r"\b(riset|research|analisis|analisa|analysis|survei|survey|kompetitor)\b", re.I
)
_REPORT = re.compile(r"\b(laporan|report|reporting)\b", re.I)
_CALENDAR = re.compile(r"\b(kalender|calendar|konten mingguan|weekly content)\b", re.I)
_WEEKS = re.compile(r"(\d{1,2})\s*(minggu|week|pekan)", re.I)


class LlmEstimate(BaseModel):
    estimated_days: float = Field(ge=0)
    confidence: float = Field(default=0.6, ge=0, le=1)
    message: str = Field(default="", max_length=400)


@dataclass(frozen=True)
class Heuristic:
    days: float
    reasons: tuple[str, ...]


def round_half(value: float) -> float:
    return max(MIN_DAYS, min(MAX_DAYS, math.ceil(value * 2) / 2))


def heuristic(title: str, description: str, subtask_count: int = 0) -> Heuristic:
    text = f"{title}\n{description}"
    days = 1.0
    reasons = ["basis 1 hari"]
    if subtask_count:
        days += 0.5 * subtask_count
        reasons.append(f"{subtask_count} subtask")
    extra = len(description) // DESCRIPTION_CHARS
    if extra:
        days += extra
        reasons.append(f"deskripsi {len(description)} karakter")
    if _RESEARCH.search(text):
        days += 1
        reasons.append("riset/analisis")
    if _CALENDAR.search(text):
        weeks = max((int(m.group(1)) for m in _WEEKS.finditer(text)), default=1)
        days += weeks
        reasons.append(f"kalender {weeks} minggu")
    if _REPORT.search(text):
        days += 1
        reasons.append("laporan")
    return Heuristic(round_half(days), tuple(reasons))


def schedule(earliest: date, estimated_days: float) -> tuple[date, date]:
    start = next_workday(earliest)
    end = add_workdays(start, max(1, math.ceil(estimated_days)))
    return start, end


def _generic_message(days: float) -> str:
    shown = int(days) if days == int(days) else days
    return f"Saya akan mengerjakan task ini. Estimasi selesai {shown} hari kerja."


async def estimate(
    inp: EstimationInput,
    llm: Any,
    *,
    budget: BudgetScope | None = None,
    timeout_s: float = LLM_TIMEOUT_S,
) -> tuple[EstimationOutput, int]:
    """Kembalikan `(output, llm_tokens)`."""
    description = inp.task_description or ""
    h = heuristic(inp.task.title, description)
    earliest = date.fromisoformat(inp.earliest_start_date)
    days, confidence, message, tokens = h.days, FALLBACK_CONFIDENCE, _generic_message(h.days), 0
    if llm is not None:
        prompt = prompts.render(
            "estimate",
            title=truncate(inp.task.title, 300),
            description=truncate(description, DESCRIPTION_CHARS) or "-",
            subtasks="-",
            heuristic_days=h.days,
        )
        try:
            parsed, result = await asyncio.wait_for(
                llm.chat_json(
                    "estimate",
                    [{"role": "user", "content": prompt}],
                    LlmEstimate,
                    budget=budget,
                    prompt_version="v1",
                ),
                timeout=timeout_s,
            )
            tokens = result.total_tokens
            days = round_half(min(max(parsed.estimated_days, h.days * 0.5), h.days * 2))
            confidence = min(1.0, max(0.0, parsed.confidence))
            # pesan LLM hanya dipakai bila angka hari tidak berubah oleh pembatasan 0,5×–2×
            clamped = abs(parsed.estimated_days - days) > 0.01
            message = (not clamped and truncate(parsed.message.strip(), 200)) or _generic_message(
                days
            )
        except (ToolError, TimeoutError) as exc:
            log.info("estimate.llm_fallback", reason=getattr(exc, "code", "TIMEOUT"))
    start, end = schedule(earliest, days)
    output = EstimationOutput(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        estimated_days=days,
        confidence=round(confidence, 2),
        message=message,
    )
    return output, tokens
