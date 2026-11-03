"""Eskalasi sentimen ke LLM `fast` dengan format baris `i|label|score` (±50% lebih hemat dari JSON).

Parser toleran (pemisah `|:;,`, label pos/positif/positive…); baris hilang/rusak diulang sekali,
setelah itu item dibiarkan kosong agar pemanggil jatuh ke label model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mcp_marketing.services.llm import prompts

SENTIMENT_BATCH = 50
ASPECT_BATCH = 40
TEXT_CHARS = 280
LABELS = {"pos": "positive", "neu": "neutral", "neg": "negative", "net": "neutral"}
_LINE = re.compile(
    r"^\s*(\d+)\s*[|:;,]\s*(pos\w*|neu\w*|net\w*|neg\w*)\s*(?:[|:;,]\s*([01](?:[.,]\d+)?))?",
    re.I,
)
_ASPECT_LINE = re.compile(
    r"^\s*(\d+)\s*[|:;,]\s*([^|:;,]+?)\s*[|:;,]\s*(pos\w*|neu\w*|net\w*|neg\w*)", re.I
)
DEFAULT_LLM_SCORE = 0.75


@dataclass
class LlmOutcome:
    labels: dict[int, tuple[str, float]]
    tokens: int
    calls: int


def _label(raw: str) -> str:
    return LABELS[raw.lower()[:3]]


def _clean(text: str) -> str:
    return " ".join(text.split())[:TEXT_CHARS].replace("|", "/")


def parse_sentiment_lines(content: str, n: int) -> dict[int, tuple[str, float]]:
    """`{index_0based: (label, score)}`; nomor di luar 1..n diabaikan."""
    out: dict[int, tuple[str, float]] = {}
    for line in (content or "").splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        idx = int(match.group(1)) - 1
        if 0 <= idx < n and idx not in out:
            score = float(match.group(3).replace(",", ".")) if match.group(3) else DEFAULT_LLM_SCORE
            out[idx] = (_label(match.group(2)), max(0.0, min(1.0, score)))
    return out


def parse_aspect_lines(content: str, n: int) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for line in (content or "").splitlines():
        match = _ASPECT_LINE.match(line)
        if match:
            idx = int(match.group(1)) - 1
            if 0 <= idx < n and idx not in out:
                out[idx] = (match.group(2).strip().lower(), _label(match.group(3)))
    return out


async def _ask(llm: Any, prompt_name: str, lines: list[str], budget: Any) -> tuple[str, int]:
    content = prompts.render(prompt_name, items="\n".join(lines))
    result = await llm.chat(
        "sentiment_fallback",
        [{"role": "user", "content": content}],
        budget=budget,
        prompt_version=prompts.tag(prompt_name),
    )
    return result.content or "", result.total_tokens


async def classify(llm: Any, texts: Sequence[str], budget: Any = None) -> LlmOutcome:
    labels: dict[int, tuple[str, float]] = {}
    tokens = calls = 0
    for start in range(0, len(texts), SENTIMENT_BATCH):
        batch = list(range(start, min(start + SENTIMENT_BATCH, len(texts))))
        for _attempt in range(2):  # percobaan kedua hanya untuk baris yang hilang/rusak
            if not batch:
                break
            lines = [f"{i + 1}|{_clean(texts[g])}" for i, g in enumerate(batch)]
            content, used = await _ask(llm, "sentiment_batch", lines, budget)
            tokens, calls = tokens + used, calls + 1
            parsed = parse_sentiment_lines(content, len(batch))
            for local, value in parsed.items():
                labels[batch[local]] = value
            batch = [g for i, g in enumerate(batch) if i not in parsed]
    return LlmOutcome(labels, tokens, calls)


async def classify_aspects(
    llm: Any, pairs: Sequence[tuple[str, str]], budget: Any = None
) -> tuple[dict[int, str], int, int]:
    """`pairs` = (aspek, kalimat) -> `({index: label}, tokens, calls)`."""
    labels: dict[int, str] = {}
    tokens = calls = 0
    for start in range(0, len(pairs), ASPECT_BATCH):
        chunk = pairs[start : start + ASPECT_BATCH]
        lines = [
            f"{i + 1}|{aspect}|{_clean(sentence)}" for i, (aspect, sentence) in enumerate(chunk)
        ]
        content, used = await _ask(llm, "aspect_batch", lines, budget)
        tokens, calls = tokens + used, calls + 1
        for local, (_aspect, label) in parse_aspect_lines(content, len(chunk)).items():
            labels[start + local] = label
    return labels, tokens, calls
