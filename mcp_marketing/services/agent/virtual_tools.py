"""Tool virtual agent runtime: menulis ke Dashboard lewat webhook (run_id disuntik runtime).

Aturan keamanan kontrak: hanya task run atau turunannya; tidak bisa mengubah assignee, menghapus,
atau menyentuh project lain; mention agent tidak disertakan (pencegahan loop).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from mcp_marketing.core.errors import (
    Forbidden,
    HierarchyDepthExceeded,
    UpstreamTimeout,
    ValidationFailed,
)
from mcp_marketing.schemas.common import ToolInput
from mcp_marketing.schemas.contract import MAX_DEPTH, TaskPriority
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.registry import ToolSpec, compact_schema
from mcp_marketing.services.dashboard.chunking import (
    chain_append_ops,
    chunk_block_ops,
    chunk_subtasks,
)
from mcp_marketing.services.dashboard.outbox import Outbox
from mcp_marketing.services.templates.md_to_blocks import markdown_to_blocks
from mcp_marketing.utils.text import truncate

if TYPE_CHECKING:
    from mcp_marketing.models import AgentProfile

UUID_RE = r"^[0-9a-fA-F-]{36}$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
COMMENT_SUMMARY_MAX = 1_500


# ------------------------------------------------------------------ input model
class ReportProgressIn(ToolInput):
    status: Literal["thinking", "working"] = "working"
    message: str = Field(max_length=120)


class BlockUpdate(BaseModel):
    block_id: str = Field(pattern=UUID_RE)
    markdown: str = Field(min_length=1, max_length=10_000)
    expected_version: int | None = Field(default=None, gt=0)


class WriteBlocksIn(ToolInput):
    markdown: str = Field(
        default="", max_length=40_000, description="Markdown ringkas yang ditambahkan"
    )
    task_id: str | None = Field(default=None, pattern=UUID_RE, description="Default: task run")
    after_block_id: str | None = Field(
        default=None, pattern=UUID_RE, description="Sisip setelah blok ini"
    )
    updates: list[BlockUpdate] = Field(
        default_factory=list,
        max_length=50,
        description="Ganti isi blok yang ada (blok pertama markdown)",
    )


class NewSubtask(BaseModel):
    client_ref: str | None = Field(default=None, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    priority: TaskPriority | None = None
    start_date: str | None = Field(default=None, pattern=DATE_RE)
    end_date: str | None = Field(default=None, pattern=DATE_RE)
    tags: list[str] | None = Field(default=None, max_length=20)
    assign_to_self: bool = False
    markdown: str | None = Field(
        default=None, max_length=8_000, description="Isi awal dokumen subtask"
    )


class CreateSubtasksIn(ToolInput):
    parent_task_id: str | None = Field(
        default=None, pattern=UUID_RE, description="Default: task run"
    )
    subtasks: list[NewSubtask] = Field(min_length=1, max_length=60)


class PostCommentIn(ToolInput):
    content: str = Field(min_length=1, max_length=10_000)
    parent_comment_id: str | None = Field(default=None, pattern=UUID_RE)
    mention_user_ids: list[str] = Field(default_factory=list, max_length=20)


class FinishIn(ToolInput):
    summary: str = Field(min_length=1, max_length=500)


class FailIn(ToolInput):
    error_code: str = Field(pattern=r"^[A-Z][A-Z_]{2,63}$")
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False


class _Out(BaseModel):
    summary: str


async def _unused(
    inp: Any, ctx: Any
) -> Any:  # pragma: no cover - dieksekusi runtime, bukan pipeline
    raise NotImplementedError


def _spec(name: str, summary: str, description: str, model: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=name,
        toolset="runtime",
        summary=summary,
        description=description,
        input_model=model,
        output_model=_Out,
        handler=_unused,
        writes=True,
    )


VIRTUAL_TOOLS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec(
            "report_progress",
            "Kirim status singkat ke Dashboard",
            "Show a short progress status on the Dashboard task badge "
            "(thinking|working, <=120 chars).",
            ReportProgressIn,
        ),
        _spec(
            "write_blocks",
            "Tulis markdown ke dokumen task",
            "Append concise markdown (headings, bullets, checklists, tables) to the task "
            "document, or "
            "replace existing blocks via updates. Default target is the run task.",
            WriteBlocksIn,
        ),
        _spec(
            "create_subtasks",
            "Buat subtask di bawah task run",
            "Create subtasks under the run task (or a subtask created in this run). Give each a "
            "client_ref; returns client_ref->id mapping. Max depth 3.",
            CreateSubtasksIn,
        ),
        _spec(
            "post_comment",
            "Kirim komentar sebagai agent",
            "Post a comment as the agent on the run task (mention runs reply in the same thread).",
            PostCommentIn,
        ),
        _spec(
            "finish",
            "Selesaikan run dengan ringkasan",
            "Finish the run successfully with a summary (<=500 chars). "
            "Call exactly once at the end.",
            FinishIn,
        ),
        _spec(
            "fail",
            "Gagalkan run dengan kode error",
            "Stop the run as failed with an UPPER_SNAKE error_code and a short message.",
            FailIn,
        ),
    )
}
WRITE_VIRTUALS = frozenset(VIRTUAL_TOOLS)


def openai_tool(spec: ToolSpec) -> dict[str, Any]:
    params = compact_schema(spec.input_model.model_json_schema())
    return {
        "type": "function",
        "function": {"name": spec.name, "description": spec.description, "parameters": params},
    }


# ------------------------------------------------------------------ state
@dataclass
class RunState:
    run_id: str
    trigger: str
    agent_key: str
    task_id: str | None
    project_id: str
    run_input: dict[str, Any]
    profile: AgentProfile
    identity: ClientIdentity
    allowed: dict[str, ToolSpec]
    max_steps: int
    route: str
    subtask_refs: dict[str, str] = field(default_factory=dict)
    known_tasks: dict[str, int] = field(default_factory=dict)  # task_id -> depth
    last_block: dict[str | None, str] = field(default_factory=dict)
    finished: str | None = None
    failed: tuple[str, str, bool] | None = None
    comment_posted: bool = False
    working_sent: bool = False

    @property
    def prompt_block_id(self) -> str | None:
        return (
            (self.run_input.get("prompt_block") or {}).get("id")
            if self.trigger == "prompt_block"
            else None
        )

    @property
    def mention_parent(self) -> str | None:
        comment = self.run_input.get("comment") or {}
        return (
            comment.get("parent_comment_id") or comment.get("id")
            if self.trigger == "mention"
            else None
        )


# ------------------------------------------------------------------ eksekusi
class VirtualTools:
    def __init__(self, outbox: Outbox, persist_refs: Any) -> None:
        self.outbox = outbox
        self._persist_refs = persist_refs

    async def execute(self, state: RunState, name: str, args: dict[str, Any]) -> dict[str, Any]:
        model = VIRTUAL_TOOLS[name].input_model
        inp = model.model_validate(args)
        handler = getattr(self, f"_{name}")
        result: dict[str, Any] = await handler(state, inp)
        return result

    async def _report_progress(self, state: RunState, inp: ReportProgressIn) -> dict[str, Any]:
        await self.outbox.emit(
            state.run_id, "agent.status.changed", {"status": inp.status, "message": inp.message}
        )
        state.working_sent = state.working_sent or inp.status == "working"
        return {"summary": "status dikirim"}

    def _check_task(self, state: RunState, task_id: str | None) -> str | None:
        if task_id is None:
            return state.task_id
        task_id = task_id.lower()
        if task_id not in state.known_tasks:
            raise Forbidden("task_id di luar cakupan run ini (hanya task run & turunannya)")
        return task_id

    async def _write_blocks(self, state: RunState, inp: WriteBlocksIn) -> dict[str, Any]:
        target = self._check_task(state, inp.task_id)
        blocks = markdown_to_blocks(inp.markdown) if inp.markdown.strip() else []
        if not blocks and not inp.updates:
            raise ValidationFailed("markdown kosong dan tidak ada updates")
        after = inp.after_block_id or state.last_block.get(target)
        if after is None and target == state.task_id and state.prompt_block_id:
            after = state.prompt_block_id
        ops = chain_append_ops(blocks, after_block_id=after)
        for upd in inp.updates:
            converted = markdown_to_blocks(upd.markdown)
            if not converted:
                raise ValidationFailed(f"markdown update kosong untuk blok {upd.block_id}")
            op: dict[str, Any] = {"op": "update", "block": {"id": upd.block_id, **converted[0]}}
            if upd.expected_version:
                op["expected_version"] = upd.expected_version
            ops.append(op)
        applied: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        pending = False
        for chunk in chunk_block_ops(ops):
            try:
                res = await self.outbox.emit_and_wait(
                    state.run_id, "agent.block.appended", {"operations": chunk}, task_id=target
                )
            except UpstreamTimeout:
                pending = True
                continue
            applied += res.get("applied") or []
            conflicts += res.get("conflicts") or []
        appended = [o for o in ops if o["op"] == "append"]
        if appended and after is not None:
            state.last_block[target] = appended[-1]["block"]["id"]
        out: dict[str, Any] = {
            "summary": f"{len(applied) or len(ops)} blok ditulis",
            "applied": len(applied),
        }
        if conflicts:
            out["conflicts"] = conflicts[:20]
        if pending:
            out["pending"] = True
        return out

    async def _create_subtasks(self, state: RunState, inp: CreateSubtasksIn) -> dict[str, Any]:
        parent = self._check_task(state, inp.parent_task_id)
        if parent is None:
            raise Forbidden("Subtask hanya dapat dibuat pada run task")
        parent_depth = state.known_tasks.get(parent, 1)
        if parent_depth + 1 > MAX_DEPTH:
            raise HierarchyDepthExceeded(f"Kedalaman subtask maksimal {MAX_DEPTH}")
        specs = []
        for i, s in enumerate(inp.subtasks):
            spec: dict[str, Any] = {
                "client_ref": s.client_ref or f"s{len(state.subtask_refs) + i + 1}",
                "title": s.title,
                "assign_to_self": s.assign_to_self,
            }
            for key in ("priority", "start_date", "end_date", "tags"):
                if getattr(s, key) is not None:
                    spec[key] = getattr(s, key)
            if s.markdown:
                spec["initial_blocks"] = markdown_to_blocks(s.markdown)[:50]
            specs.append(spec)
        created: list[dict[str, Any]] = []
        for chunk in chunk_subtasks(specs):
            data: dict[str, Any] = {"subtasks": chunk}
            if parent != state.task_id:
                data["parent_task_id"] = parent
            res = await self.outbox.emit_and_wait(state.run_id, "agent.subtask.created", data)
            created += res.get("created") or []
        for c in created:
            if c.get("id"):
                state.known_tasks[str(c["id"]).lower()] = parent_depth + 1
                if c.get("client_ref"):
                    state.subtask_refs[str(c["client_ref"])] = str(c["id"])
        await self._persist_refs(state.run_id, state.subtask_refs)
        return {
            "summary": f"{len(created)} subtask dibuat",
            "created": [{"client_ref": c.get("client_ref"), "id": c.get("id")} for c in created],
        }

    async def _post_comment(self, state: RunState, inp: PostCommentIn) -> dict[str, Any]:
        data: dict[str, Any] = {"content": inp.content}
        parent = inp.parent_comment_id or state.mention_parent
        if parent:
            data["parent_comment_id"] = parent
        users = [u for u in inp.mention_user_ids if _is_uuid(u)]
        if users:
            data["mentions"] = [{"type": "user", "id": u} for u in users]
        res = await self.outbox.emit_and_wait(state.run_id, "agent.comment.added", data)
        state.comment_posted = True
        return {"summary": "komentar terkirim", "comment_id": res.get("comment_id")}

    async def _finish(self, state: RunState, inp: FinishIn) -> dict[str, Any]:
        state.finished = inp.summary
        return {"summary": "run akan diselesaikan"}

    async def _fail(self, state: RunState, inp: FailIn) -> dict[str, Any]:
        state.failed = (inp.error_code, inp.message, inp.retryable)
        return {"summary": "run akan digagalkan"}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def completion_comment(summary: str) -> str:
    return truncate(f"Selesai. {summary}", COMMENT_SUMMARY_MAX)
