"""Registry run Dashboard di MCP (`mcp.runs`): idempotensi dispatch, batas mention, cancel."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import orjson
from sqlalchemy import func, select, text, update

from mcp_marketing.core.database import Database
from mcp_marketing.core.errors import Conflict, RateLimited
from mcp_marketing.core.logging import get_logger
from mcp_marketing.core.metrics import RUNS_TOTAL
from mcp_marketing.models import AgentProfile, Run, WebhookOutbox
from mcp_marketing.schemas.contract import (
    ContextBlock,
    DispatchInput,
    MentionInput,
    PromptBlockInput,
)
from mcp_marketing.utils.ids import new_mcp_run_id
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import now

log = get_logger(__name__)

ACTIVE = ("accepted", "running")
TERMINAL = ("completed", "failed", "cancelled", "cancelled_by_dashboard")
MAX_INPUT_BYTES = 100_000
MENTION_LIMIT_PER_HOUR = 10
CONTEXT_TEXT_MAX = 400
THREAD_KEEP = 10
THREAD_TEXT_MAX = 1_000

RunInput = DispatchInput | MentionInput | PromptBlockInput


@dataclass(frozen=True)
class AcceptResult:
    run_id: str
    mcp_run_id: str
    created: bool
    # run lama yang belum pernah mengirim event (mis. crash setelah commit sebelum emit)
    needs_start: bool = False


def compact_context(blocks: list[ContextBlock], budget_bytes: int = 60_000) -> list[dict[str, Any]]:
    """context_blocks -> outline teks (id, tipe, teks ≤ 400 char), dibatasi ukuran."""
    out: list[dict[str, Any]] = []
    size = 0
    for b in blocks:
        item: dict[str, Any] = {
            "id": b.id,
            "type": b.block_type,
            "text": truncate(b.plain_text, CONTEXT_TEXT_MAX),
        }
        if b.block_type == "heading" and isinstance(b.content_json, dict):
            item["level"] = (b.content_json.get("props") or {}).get("level", 1)
        if b.parent_block_id:
            item["parent"] = b.parent_block_id
        size += len(orjson.dumps(item))
        if size > budget_bytes:
            break
        out.append(item)
    return out


def stored_input(trigger: str, inp: RunInput) -> dict[str, Any]:
    """Input run yang disimpan (dipangkas; maks 100 KB)."""
    base: dict[str, Any] = {
        "trigger": trigger,
        "contract_version": inp.contract_version,
        "agent_key": inp.agent_key,
        "context": compact_context(inp.context_blocks),
    }
    task = getattr(inp, "task", None)
    if task is not None:
        base["task"] = task.model_dump(mode="json")
    if isinstance(inp, DispatchInput):
        base["description"] = truncate(inp.task_description, 4_000)
        base["subtasks"] = [s.model_dump(mode="json") for s in inp.subtasks[:50]]
        base["dispatched_by"] = (
            inp.dispatched_by.model_dump(mode="json") if inp.dispatched_by else None
        )
    elif isinstance(inp, MentionInput):
        base["comment"] = {
            **inp.comment.model_dump(mode="json"),
            "content": truncate(inp.comment.content, THREAD_TEXT_MAX),
        }
        base["thread"] = [
            {**t.model_dump(mode="json"), "content": truncate(t.content, THREAD_TEXT_MAX)}
            for t in inp.thread[-THREAD_KEEP:]
        ]
    elif isinstance(inp, PromptBlockInput):
        base["prompt_block"] = inp.prompt_block.model_dump(mode="json")
    while len(orjson.dumps(base)) > MAX_INPUT_BYTES and base["context"]:
        base["context"] = base["context"][: len(base["context"]) // 2]
    return base


class RunRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def accept(self, trigger: str, inp: RunInput, profile: AgentProfile) -> AcceptResult:
        run_uuid = uuid.UUID(inp.run_id)
        task = getattr(inp, "task", None)
        project_id = task.project_id if task is not None else getattr(inp, "project_id", None)
        task_id = task.id if task is not None else None
        workspace_id = task.workspace_id if task is not None else None
        async with self.db.session() as session:
            # serialisasi dispatch bersamaan untuk run_id yang sama (hindari bentrok primary key)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": inp.run_id}
            )
            existing = await session.scalar(
                select(Run).where(Run.run_id == run_uuid).with_for_update()
            )
            if existing is not None:
                if existing.status in ACTIVE:
                    events = await session.scalar(
                        select(func.count())
                        .select_from(WebhookOutbox)
                        .where(WebhookOutbox.run_id == run_uuid)
                    )
                    return AcceptResult(
                        str(existing.run_id),
                        existing.mcp_run_id,
                        created=False,
                        needs_start=existing.status == "accepted" and not events,
                    )
                raise Conflict(
                    f"Run {inp.run_id} sudah {existing.status}; buat run baru", retryable=False
                )
            if trigger == "mention":
                await self._check_mention_rate(session, task_id or project_id)
            run = Run(
                run_id=run_uuid,
                mcp_run_id=new_mcp_run_id(),
                trigger=trigger,
                agent_key=profile.agent_key,
                agent_id=uuid.UUID(inp.agent_id),
                workspace_id=uuid.UUID(workspace_id) if workspace_id else None,
                project_id=uuid.UUID(str(project_id)),
                task_id=uuid.UUID(task_id) if task_id else None,
                status="accepted",
                input=stored_input(trigger, inp),
                callback=inp.callback.model_dump(mode="json"),
                token_budget=profile.run_token_budget,
            )
            session.add(run)
        RUNS_TOTAL.labels(trigger, "accepted").inc()
        log.info("run.accepted", run_id=inp.run_id, trigger=trigger, agent_key=profile.agent_key)
        return AcceptResult(inp.run_id, run.mcp_run_id, created=True)

    @staticmethod
    async def _check_mention_rate(session: Any, scope_id: str | None) -> None:
        if not scope_id:
            return
        target = uuid.UUID(scope_id)
        since = now() - timedelta(hours=1)
        count = await session.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.trigger == "mention",
                Run.created_at >= since,
                (Run.task_id == target) | ((Run.task_id.is_(None)) & (Run.project_id == target)),
            )
        )
        if (count or 0) >= MENTION_LIMIT_PER_HOUR:
            raise RateLimited(
                f"Maksimal {MENTION_LIMIT_PER_HOUR} run mention per task per jam",
                details={"limit": MENTION_LIMIT_PER_HOUR},
            )

    async def get(self, run_id: str) -> Run | None:
        async with self.db.session() as session:
            return await session.get(Run, uuid.UUID(run_id))

    async def mark_cancelled(self, run_id: str, reason: str) -> bool:
        """True bila run dikenal (aktif maupun tidak)."""
        async with self.db.session() as session:
            run = await session.get(Run, uuid.UUID(run_id))
            if run is None:
                return False
            run.cancel_requested = True
            run.cancel_reason = reason[:32]
            if run.status in ACTIVE:
                run.status = "cancelled"
                run.finished_at = now()
                RUNS_TOTAL.labels(run.trigger, "cancelled").inc()
        return True

    async def update(self, run_id: str, **values: Any) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Run).where(Run.run_id == uuid.UUID(run_id)).values(**values)
            )
