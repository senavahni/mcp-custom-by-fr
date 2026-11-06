"""Riwayat percakapan agent: persistensi `run_steps` (untuk resume) + kompaksi riwayat.

Baris `run_steps`: `step` = nomor urut baris per run; `role` = assistant | tool | summary | user.
Kompaksi: pesan di antara brief dan 2 langkah terakhir diganti ringkasan `fast` (≤ 300 token)
yang mempertahankan handle/job/report id (MCP_SPEC §8, TASK-10).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import orjson
from sqlalchemy import func, select

from mcp_marketing.core.database import Database
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models import RunStep
from mcp_marketing.services.llm import prompts
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.tokens import count_tokens

log = get_logger(__name__)

SUMMARY_MAX_TOKENS = 300
KEEP_TAIL_STEPS = 2
HISTORY_LINE_MAX = 1_500
_ID_RE = re.compile(r"\b(?:h|j|r|mr)_[0-9a-z]{10}\b")


class RunMemory:
    def __init__(self, db: Database, run_id: str) -> None:
        self.db = db
        self.run_id = run_id
        self._next_step: int | None = None

    async def _step_no(self) -> int:
        if self._next_step is None:
            async with self.db.session() as session:
                last = await session.scalar(
                    select(func.max(RunStep.step)).where(RunStep.run_id == uuid.UUID(self.run_id))
                )
            self._next_step = int(last or 0) + 1
        step = self._next_step
        self._next_step += 1
        return step

    async def save(
        self, role: str, content: dict[str, Any], *, tool: str | None = None, tokens: int = 0
    ) -> None:
        step = await self._step_no()
        async with self.db.session() as session:
            session.add(
                RunStep(
                    run_id=uuid.UUID(self.run_id),
                    step=step,
                    role=role,
                    tool=tool,
                    content=orjson.loads(orjson.dumps(content, default=str)),
                    tokens=tokens,
                )
            )

    async def load(self) -> list[RunStep]:
        async with self.db.session() as session:
            rows = await session.scalars(
                select(RunStep)
                .where(RunStep.run_id == uuid.UUID(self.run_id))
                .order_by(RunStep.step)
            )
            return list(rows.all())


def messages_from_steps(head: list[dict[str, Any]], steps: list[RunStep]) -> list[dict[str, Any]]:
    """Rekonstruksi pesan untuk resume. Ringkasan menggantikan semua langkah sebelumnya."""
    body: list[dict[str, Any]] = []
    for row in steps:
        if row.role == "summary":
            body = [summary_message(str(row.content.get("text", "")))]
            body += [m for m in row.content.get("tail", [])]
        elif row.role == "assistant":
            body.append(_assistant_message(row.content))
        elif row.role == "tool":
            body.append(
                {
                    "role": "tool",
                    "tool_call_id": row.content.get("tool_call_id"),
                    "content": row.content.get("content", ""),
                }
            )
        elif row.role == "user":
            body.append({"role": "user", "content": row.content.get("content", "")})
    return head + _drop_dangling(body)


def _assistant_message(content: dict[str, Any]) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content.get("content")}
    calls = content.get("tool_calls") or []
    if calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": orjson.dumps(c.get("arguments") or {}).decode(),
                },
            }
            for c in calls
        ]
    return msg


def _drop_dangling(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Buang assistant (dan hasil tool parsialnya) yang tool_call-nya tidak terjawab lengkap.

    Terjadi bila server mati di tengah eksekusi tool; langkah SESUDAHNYA (setelah resume) tetap
    dipertahankan agar resume berikutnya tidak kehilangan konteks.
    """
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(body):
        msg = body[i]
        calls = msg.get("tool_calls") or []
        if msg.get("role") != "assistant" or not calls:
            if msg.get("role") != "tool":  # tool yatim (tanpa assistant) dibuang
                out.append(msg)
            i += 1
            continue
        j = i + 1
        while j < len(body) and body[j].get("role") == "tool":
            j += 1
        answered = {m.get("tool_call_id") for m in body[i + 1 : j]}
        if all(c["id"] in answered for c in calls):
            out.extend(body[i:j])
        i = j
    return out


def summary_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": f"[Ringkasan langkah sebelumnya]\n{text}"}


def split_for_compaction(
    messages: list[dict[str, Any]], head_len: int, keep_steps: int = KEEP_TAIL_STEPS
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pisah `(tengah, ekor)`: ekor dimulai pada pesan assistant ke-`keep_steps` dari belakang."""
    body = messages[head_len:]
    starts = [i for i, m in enumerate(body) if m.get("role") == "assistant"]
    if len(starts) <= keep_steps:
        return [], body
    cut = starts[-keep_steps]
    return body[:cut], body[cut:]


def history_text(middle: list[dict[str, Any]]) -> str:
    lines = []
    for m in middle:
        role = m.get("role")
        if role == "assistant":
            calls = ", ".join(
                f"{c['function']['name']}({truncate(c['function']['arguments'], 200)})"
                for c in m.get("tool_calls") or []
            )
            text = m.get("content") or ""
            lines.append(
                f"ASSISTANT: {truncate(text, 400)} {('-> ' + calls) if calls else ''}".strip()
            )
        elif role == "tool":
            lines.append(f"TOOL: {truncate(str(m.get('content', '')), HISTORY_LINE_MAX)}")
        else:
            lines.append(
                f"{str(role).upper()}: {truncate(str(m.get('content', '')), HISTORY_LINE_MAX)}"
            )
    return "\n".join(lines)


async def compact_history(
    llm: Any,
    messages: list[dict[str, Any]],
    head_len: int,
    *,
    budget: Any = None,
    route: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, int]:
    """`(pesan_baru, teks_ringkasan, token)`; tanpa perubahan bila tak ada bagian tengah.
    `route` = route agent khusus (None = route `fast` bawaan purpose compact)."""
    middle, tail = split_for_compaction(messages, head_len)
    if not middle:
        return messages, None, 0
    history = history_text(middle)
    ids = sorted(set(_ID_RE.findall(history)))
    prompt = prompts.render("compact_history", max_tokens=SUMMARY_MAX_TOKENS, history=history)
    result = await llm.chat(
        "compact",
        [{"role": "user", "content": prompt}],
        max_tokens=SUMMARY_MAX_TOKENS,
        route_override=route,
        budget=budget,
        prompt_version="v1",
    )
    summary = (result.content or "").strip()
    missing = [i for i in ids if i not in summary]
    if missing:
        summary += "\nID relevan: " + ", ".join(missing)
    new_messages = messages[:head_len] + [summary_message(summary)] + tail
    log.info(
        "agent.history_compacted", before=count_tokens(messages), after=count_tokens(new_messages)
    )
    return new_messages, summary, result.total_tokens
