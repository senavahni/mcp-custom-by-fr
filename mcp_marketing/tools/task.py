"""Task/subtask tools: read from Dashboard's read-only schema; write via webhook or Service API."""

from __future__ import annotations

import base64
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, cast, func, or_, select

from mcp_marketing.core.errors import (
    Forbidden,
    HierarchyDepthExceeded,
    NotFound,
    ValidationFailed,
)
from mcp_marketing.models import Run
from mcp_marketing.models.dashboard import (
    Comment,
    DashboardAgent,
    DashboardUser,
    DocumentBlock,
    Project,
    Task,
)
from mcp_marketing.schemas.blocks import validate_block_content
from mcp_marketing.schemas.common import ReadInput, ToolInput
from mcp_marketing.schemas.contract import InitialBlock
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.dashboard.blocks_to_md import block_to_markdown, blocks_to_markdown
from mcp_marketing.services.llm.prompts import render as render_prompt
from mcp_marketing.services.tasks.scheduler import schedule_subtasks
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import today_in

MAX_DEPTH = 3
TaskListField = Literal["id", "title", "status", "priority", "start", "end", "depth", "assignee"]
TaskInclude = Literal["blocks_outline", "blocks_full", "subtasks", "comments"]


def _default_task_fields() -> list[TaskListField]:
    return ["id", "title", "status", "priority", "start", "end", "depth", "assignee"]


def _default_task_includes() -> list[TaskInclude]:
    return ["blocks_outline"]


class TaskListInput(ReadInput):
    project_id: uuid.UUID
    status: list[Literal["todo", "in_progress", "in_review", "done"]] = Field(
        default_factory=list, max_length=4
    )
    parent_task_id: uuid.UUID | None = None
    assignee: str | None = Field(default=None, max_length=100)
    due_before: date | None = None
    tag: str | None = Field(default=None, max_length=50)
    fields: list[TaskListField] = Field(default_factory=_default_task_fields)
    limit: int = Field(default=20, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=100)


class TaskListOutput(BaseModel):
    summary: str
    cols: list[str]
    rows: list[list[Any]]
    total: int
    next_cursor: str | None = None


class TaskGetInput(ReadInput):
    task_id: uuid.UUID
    include: list[TaskInclude] = Field(default_factory=_default_task_includes, max_length=4)


class TaskGetOutput(BaseModel):
    summary: str
    task: dict[str, Any]


class CreateItem(ToolInput):
    client_ref: str | None = Field(default=None, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    priority: Literal["none", "low", "medium", "high", "urgent"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    is_milestone: bool = False
    initial_blocks: list[InitialBlock] | None = Field(default=None, max_length=50)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title minimal 1 karakter")
        return cleaned

    @model_validator(mode="after")
    def validate_task(self) -> CreateItem:
        if self.tags and any(not tag.strip() or len(tag) > 50 for tag in self.tags):
            raise ValueError("tag harus berisi 1–50 karakter")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date harus >= start_date")
        if self.is_milestone and (
            self.start_date is None or self.end_date is None or self.start_date != self.end_date
        ):
            raise ValueError("Milestone wajib memiliki start_date=end_date")
        return self


class TaskCreateInput(ToolInput):
    project_id: uuid.UUID
    parent_task_id: uuid.UUID | None = None
    tasks: list[CreateItem] = Field(min_length=1, max_length=20)


class TaskWriteOutput(BaseModel):
    summary: str
    id: str | None = None
    created: list[dict[str, Any]] = Field(default_factory=list)
    updated_fields: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)


class TaskUpdateInput(ToolInput):
    task_id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["todo", "in_progress", "in_review", "done"] | None = None
    priority: Literal["none", "low", "medium", "high", "urgent"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    is_milestone: bool | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title minimal 1 karakter")
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def reject_assignee_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(key.startswith("assignee") for key in value):
            raise ValueError("assignee hanya bisa diubah manusia")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> TaskUpdateInput:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date harus >= start_date")
        if (
            self.is_milestone
            and (self.start_date is not None or self.end_date is not None)
            and (
                self.start_date is None or self.end_date is None or self.start_date != self.end_date
            )
        ):
            raise ValueError("Milestone wajib memiliki start_date=end_date")
        if self.tags and any(not tag.strip() or len(tag) > 50 for tag in self.tags):
            raise ValueError("tag harus berisi 1–50 karakter")
        return self


class TaskMoveInput(ToolInput):
    task_id: uuid.UUID
    new_parent_task_id: uuid.UUID | None = None
    before_task_id: uuid.UUID | None = None


class TaskRescheduleInput(ToolInput):
    task_id: uuid.UUID
    start_date: date
    end_date: date
    cascade: bool

    @model_validator(mode="after")
    def validate_dates(self) -> TaskRescheduleInput:
        if self.end_date < self.start_date:
            raise ValueError("end_date harus >= start_date")
        return self


class TaskCommentInput(ToolInput):
    task_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    content: str = Field(min_length=1, max_length=10_000)
    parent_comment_id: uuid.UUID | None = None
    mentions: list[dict[str, str]] | None = Field(default=None, max_length=50)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content minimal 1 karakter")
        return cleaned

    @model_validator(mode="after")
    def one_target(self) -> TaskCommentInput:
        if (self.task_id is None) == (self.project_id is None):
            raise ValueError("Isi tepat satu task_id atau project_id")
        for mention in self.mentions or []:
            if mention.get("type") not in {"user", "agent"}:
                raise ValueError("Mention type harus user atau agent")
            try:
                uuid.UUID(mention.get("id", ""))
            except ValueError:
                raise ValueError("Mention id harus UUID") from None
        return self


class BreakdownInput(ToolInput):
    task_id: uuid.UUID
    max_subtasks: int = Field(default=6, ge=1, le=20)
    granularity: Literal["day", "deliverable"] = "deliverable"
    mode: Literal["preview", "apply"] = "preview"


class BreakdownItem(BaseModel):
    client_ref: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    duration_days: int = Field(ge=1, le=60)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    checklist: list[str] = Field(default_factory=list, max_length=20)


class BreakdownPlan(BaseModel):
    subtasks: list[BreakdownItem] = Field(min_length=1, max_length=20)


class BreakdownOutput(BaseModel):
    summary: str
    subtasks: list[dict[str, Any]]
    total_duration_days: int
    outside_parent_range: bool = False
    created: list[dict[str, Any]] = Field(default_factory=list)


def _cursor_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode() + b"==").decode())
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailed("Cursor task tidak valid") from None
    if offset < 0:
        raise ValidationFailed("Cursor task tidak valid")
    return offset


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


async def _run(ctx: ToolContext) -> Run | None:
    if not ctx.run_id:
        return None
    async with ctx.services.db.session() as session:
        run = await session.get(Run, uuid.UUID(ctx.run_id))
    if run is None:
        raise Forbidden("Run tidak dikenal")
    return run


async def _workspace_id(ctx: ToolContext, run: Run | None) -> uuid.UUID:
    workspace_id = str(run.workspace_id) if run and run.workspace_id else ctx.client.workspace_id
    if not workspace_id:
        raise Forbidden("Klien MCP belum dibatasi ke workspace Dashboard")
    try:
        return uuid.UUID(workspace_id)
    except ValueError:
        raise Forbidden("Workspace klien tidak valid") from None


async def _scoped_task(ctx: ToolContext, task_id: uuid.UUID, run: Run | None = None) -> Task:
    workspace_id = await _workspace_id(ctx, run)
    async with ctx.services.db.session() as session:
        task = await session.scalar(
            select(Task)
            .join(Project, Project.id == Task.project_id)
            .where(
                Task.id == task_id,
                Task.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                Project.workspace_id == workspace_id,
            )
        )
    if task is None:
        raise NotFound("Task tidak ditemukan")
    return task


async def _ensure_run_task(ctx: ToolContext, task_id: uuid.UUID, run: Run) -> Task:
    task = await _scoped_task(ctx, task_id, run)
    if run.task_id is None:
        raise Forbidden("Run ini tidak memiliki task untuk ditulis")
    current: Task | None = task
    visited: set[uuid.UUID] = set()
    while current is not None and current.id not in visited:
        if current.id == run.task_id:
            return task
        visited.add(current.id)
        if current.parent_task_id is None:
            break
        current = await _scoped_task(ctx, current.parent_task_id, run)
    raise Forbidden("Task berada di luar cakupan run ini (hanya task run dan turunannya)")


def _agent_id(ctx: ToolContext, run: Run | None) -> str:
    agent_id = str(run.agent_id) if run else ctx.client.dashboard_agent_id
    if not agent_id:
        raise Forbidden("Klien MCP belum dikonfigurasi dengan dashboard_agent_id")
    return agent_id


def _task_spec(item: CreateItem) -> dict[str, Any]:
    return item.model_dump(mode="json", exclude_none=True)


async def _create_tasks(
    ctx: ToolContext,
    *,
    project_id: uuid.UUID,
    parent_task_id: uuid.UUID | None,
    tasks: list[dict[str, Any]],
    run: Run | None,
) -> list[dict[str, Any]]:
    if run is not None:
        if parent_task_id is None:
            parent_task_id = run.task_id
        if parent_task_id is None:
            raise Forbidden("Run tanpa task tidak dapat membuat subtask")
        parent = await _ensure_run_task(ctx, parent_task_id, run)
        if parent.project_id != project_id:
            raise ValidationFailed("Project task harus sama dengan project parent")
        if parent.depth + 1 > MAX_DEPTH:
            raise HierarchyDepthExceeded(f"Kedalaman subtask maksimal {MAX_DEPTH}")
        if any(task.get("is_milestone") for task in tasks):
            if ctx.services.service_api is None:
                raise ValidationFailed(
                    "Pembuatan milestone dalam run memerlukan Dashboard Service API"
                )
            result = await ctx.services.service_api.create_subtasks(
                str(run.agent_id), str(parent_task_id), tasks
            )
            return result.get("created") or []
        if ctx.services.outbox is None:
            raise ValidationFailed("Webhook outbox tidak dikonfigurasi")
        created: list[dict[str, Any]] = []
        from mcp_marketing.services.dashboard.chunking import chunk_subtasks

        event_tasks = [
            {key: value for key, value in task.items() if key != "is_milestone"} for task in tasks
        ]
        for chunk in chunk_subtasks(event_tasks):
            result = await ctx.services.outbox.emit_and_wait(
                str(run.run_id),
                "agent.subtask.created",
                {"parent_task_id": str(parent_task_id), "subtasks": chunk},
            )
            created.extend(result.get("created") or [])
        return created
    if ctx.services.service_api is None:
        raise ValidationFailed("Dashboard Service API tidak dikonfigurasi")
    agent_id = _agent_id(ctx, None)
    result = (
        await ctx.services.service_api.create_subtasks(agent_id, str(parent_task_id), tasks)
        if parent_task_id
        else await ctx.services.service_api.create_tasks(agent_id, str(project_id), tasks)
    )
    return result.get("created") or []


@tool(
    name="task_list",
    toolset="task",
    summary="Daftar task Dashboard secara ringkas",
    description="Read-only workspace-scoped task list (maksimum 20 baris) dengan cursor dan "
    "filter status, parent, assignee, tag, dan tenggat.",
    input_model=TaskListInput,
    output_model=TaskListOutput,
    cache_ttl_s=15,
    default_fields=("id", "title", "status", "priority", "start", "end", "depth", "assignee"),
)
async def task_list(inp: TaskListInput, ctx: ToolContext) -> TaskListOutput:
    run = await _run(ctx)
    workspace_id = await _workspace_id(ctx, run)
    query = (
        select(
            Task,
            func.coalesce(DashboardUser.display_name, DashboardAgent.handle).label("assignee"),
        )
        .join(Project, Project.id == Task.project_id)
        .outerjoin(DashboardUser, DashboardUser.id == Task.assignee_user_id)
        .outerjoin(DashboardAgent, DashboardAgent.id == Task.assignee_agent_id)
        .where(
            Task.project_id == inp.project_id,
            Task.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            Project.workspace_id == workspace_id,
        )
    )
    if inp.status:
        query = query.where(Task.status.in_(inp.status))
    if inp.parent_task_id:
        query = query.where(Task.parent_task_id == inp.parent_task_id)
    if inp.due_before:
        query = query.where(Task.end_date <= inp.due_before)
    if inp.tag:
        query = query.where(Task.tags.contains([inp.tag]))
    if inp.assignee:
        if inp.assignee == "none":
            query = query.where(Task.assignee_type.is_(None))
        else:
            needle = f"%{inp.assignee}%"
            query = query.where(
                or_(
                    cast(Task.assignee_user_id, String).ilike(needle),
                    cast(Task.assignee_agent_id, String).ilike(needle),
                    DashboardUser.display_name.ilike(needle),
                    DashboardAgent.handle.ilike(needle),
                )
            )
    offset = _cursor_offset(inp.cursor)
    async with ctx.services.db.session() as session:
        total = await session.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        )
        results = (
            await session.execute(query.order_by(Task.id).offset(offset).limit(inp.limit + 1))
        ).all()
    has_more = len(results) > inp.limit
    results = results[: inp.limit]
    by_field = {
        "id": lambda task, assignee: str(task.id),
        "title": lambda task, assignee: task.title,
        "status": lambda task, assignee: task.status,
        "priority": lambda task, assignee: task.priority,
        "start": lambda task, assignee: task.start_date.isoformat() if task.start_date else None,
        "end": lambda task, assignee: task.end_date.isoformat() if task.end_date else None,
        "depth": lambda task, assignee: task.depth,
        "assignee": lambda task, assignee: assignee,
    }
    return TaskListOutput(
        summary=f"{len(results)} task dari {total or 0}",
        cols=inp.fields,
        rows=[
            [by_field[field](task, assignee) for field in inp.fields] for task, assignee in results
        ],
        total=int(total or 0),
        next_cursor=_encode_cursor(offset + inp.limit) if has_more else None,
    )


@tool(
    name="task_get",
    toolset="task",
    summary="Ambil detail task Dashboard",
    description="Read-only snapshot task scoped ke workspace/run; include blok sebagai outline "
    "atau Markdown, subtask dua level, dan 10 komentar terakhir.",
    input_model=TaskGetInput,
    output_model=TaskGetOutput,
)
async def task_get(inp: TaskGetInput, ctx: ToolContext) -> TaskGetOutput:
    run = await _run(ctx)
    task = await _scoped_task(ctx, inp.task_id, run)
    workspace_id = await _workspace_id(ctx, run)
    payload: dict[str, Any] = {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "depth": task.depth,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "end_date": task.end_date.isoformat() if task.end_date else None,
        "is_milestone": task.is_milestone,
        "tags": task.tags or [],
    }
    includes = set(inp.include)
    async with ctx.services.db.session() as session:
        if includes & {"blocks_outline", "blocks_full"}:
            blocks = list(
                (
                    await session.scalars(
                        select(DocumentBlock)
                        .where(
                            DocumentBlock.task_id == task.id,
                            DocumentBlock.deleted_at.is_(None),
                        )
                        .order_by(DocumentBlock.order_index, DocumentBlock.id)
                    )
                ).all()
            )
            if "blocks_full" in includes:
                payload["blocks_full"] = truncate(blocks_to_markdown(blocks), 12_000)
            if "blocks_outline" in includes:
                lines = []
                for block in blocks:
                    text = block_to_markdown(block.block_type, block.content_json)
                    lines.append(text if block.block_type == "heading" else truncate(text, 120))
                    if len(lines) >= 40:
                        break
                payload["blocks_outline"] = lines
        if "subtasks" in includes:
            children = list(
                (
                    await session.scalars(
                        select(Task)
                        .join(Project, Project.id == Task.project_id)
                        .where(
                            Task.parent_task_id == task.id,
                            Task.deleted_at.is_(None),
                            Project.workspace_id == workspace_id,
                        )
                        .order_by(Task.start_date, Task.id)
                    )
                ).all()
            )
            subtree = []
            for child in children:
                grandchildren = list(
                    (
                        await session.scalars(
                            select(Task)
                            .join(Project, Project.id == Task.project_id)
                            .where(
                                Task.parent_task_id == child.id,
                                Task.deleted_at.is_(None),
                                Project.workspace_id == workspace_id,
                                Project.deleted_at.is_(None),
                            )
                        )
                    ).all()
                )
                subtree.append(
                    {
                        "id": str(child.id),
                        "title": child.title,
                        "status": child.status,
                        "depth": child.depth,
                        "subtasks": [
                            {
                                "id": str(grandchild.id),
                                "title": grandchild.title,
                                "status": grandchild.status,
                                "depth": grandchild.depth,
                            }
                            for grandchild in grandchildren
                        ],
                    }
                )
            payload["subtasks"] = subtree
        if "comments" in includes:
            comments = list(
                (
                    await session.scalars(
                        select(Comment)
                        .where(
                            Comment.entity_type == "task",
                            Comment.entity_id == task.id,
                            Comment.deleted_at.is_(None),
                        )
                        .order_by(Comment.created_at.desc())
                        .limit(10)
                    )
                ).all()
            )
            payload["comments"] = [
                {
                    "id": str(comment.id),
                    "content": truncate(comment.content, 300),
                    "author_type": comment.author_type,
                    "created_at": comment.created_at.isoformat(),
                }
                for comment in comments
            ]
    return TaskGetOutput(summary=f"Task: {task.title}", task=payload)


@tool(
    name="task_create",
    toolset="task",
    summary="Buat task atau subtask Dashboard",
    description="Buat sampai 20 task. Dalam run hanya di bawah task run/turunannya; di luar run "
    "menggunakan Dashboard Service API.",
    input_model=TaskCreateInput,
    output_model=TaskWriteOutput,
    writes=True,
)
async def task_create(inp: TaskCreateInput, ctx: ToolContext) -> TaskWriteOutput:
    run = await _run(ctx)
    parent = inp.parent_task_id or (run.task_id if run else None)
    tasks = [_task_spec(item) for item in inp.tasks]
    created = await _create_tasks(
        ctx, project_id=inp.project_id, parent_task_id=parent, tasks=tasks, run=run
    )
    return TaskWriteOutput(summary=f"{len(created)} task dibuat", created=created)


@tool(
    name="task_update",
    toolset="task",
    summary="Perbarui status, jadwal, atau metadata task",
    description="Ubah field task yang diizinkan. Assignee tidak dapat diubah lewat MCP.",
    input_model=TaskUpdateInput,
    output_model=TaskWriteOutput,
    writes=True,
)
async def task_update(inp: TaskUpdateInput, ctx: ToolContext) -> TaskWriteOutput:
    run = await _run(ctx)
    if run:
        await _ensure_run_task(ctx, inp.task_id, run)
    else:
        await _scoped_task(ctx, inp.task_id)
    patch = inp.model_dump(mode="json", exclude_unset=True, exclude={"task_id"})
    if not patch:
        raise ValidationFailed("Tidak ada field task untuk diperbarui")
    if ctx.services.service_api is None:
        raise ValidationFailed("Dashboard Service API tidak dikonfigurasi")
    result = await ctx.services.service_api.update_task(
        _agent_id(ctx, run), str(inp.task_id), patch
    )
    return TaskWriteOutput(
        summary="Task diperbarui",
        id=str(inp.task_id),
        updated_fields=sorted(patch),
        result=result,
    )


@tool(
    name="task_move",
    toolset="task",
    summary="Pindahkan task dalam hierarki",
    description="Pindahkan task dengan validasi Dashboard terhadap siklus dan batas kedalaman.",
    input_model=TaskMoveInput,
    output_model=TaskWriteOutput,
    writes=True,
)
async def task_move(inp: TaskMoveInput, ctx: ToolContext) -> TaskWriteOutput:
    run = await _run(ctx)
    if run:
        task = await _ensure_run_task(ctx, inp.task_id, run)
        if inp.new_parent_task_id:
            await _ensure_run_task(ctx, inp.new_parent_task_id, run)
        if inp.before_task_id:
            await _ensure_run_task(ctx, inp.before_task_id, run)
        if inp.new_parent_task_id is None and str(task.id) != str(run.task_id):
            raise Forbidden("Task turunan tidak dapat dipindahkan keluar dari cakupan run")
    else:
        await _scoped_task(ctx, inp.task_id)
        if inp.new_parent_task_id:
            await _scoped_task(ctx, inp.new_parent_task_id)
    if ctx.services.service_api is None:
        raise ValidationFailed("Dashboard Service API tidak dikonfigurasi")
    result = await ctx.services.service_api.move_task(
        _agent_id(ctx, run),
        str(inp.task_id),
        {
            "new_parent_task_id": str(inp.new_parent_task_id) if inp.new_parent_task_id else None,
            "before_task_id": str(inp.before_task_id) if inp.before_task_id else None,
        },
    )
    return TaskWriteOutput(summary="Task dipindahkan", id=str(inp.task_id), result=result)


@tool(
    name="task_reschedule",
    toolset="task",
    summary="Jadwalkan ulang task dan dependensinya",
    description="Ubah tanggal task; opsi cascade menerapkan jadwal ke subtask melalui Timeline "
    "Service Dashboard.",
    input_model=TaskRescheduleInput,
    output_model=TaskWriteOutput,
    writes=True,
)
async def task_reschedule(inp: TaskRescheduleInput, ctx: ToolContext) -> TaskWriteOutput:
    run = await _run(ctx)
    if run:
        await _ensure_run_task(ctx, inp.task_id, run)
    else:
        await _scoped_task(ctx, inp.task_id)
    if ctx.services.service_api is None:
        raise ValidationFailed("Dashboard Service API tidak dikonfigurasi")
    result = await ctx.services.service_api.reschedule_task(
        _agent_id(ctx, run),
        str(inp.task_id),
        {
            "start_date": inp.start_date.isoformat(),
            "end_date": inp.end_date.isoformat(),
            "cascade": inp.cascade,
        },
    )
    return TaskWriteOutput(summary="Jadwal task diperbarui", id=str(inp.task_id), result=result)


@tool(
    name="task_comment",
    toolset="task",
    summary="Tambahkan komentar pada task atau project",
    description="Tambahkan komentar dan mention user (notifikasi); mention agent tidak memulai "
    "run baru.",
    input_model=TaskCommentInput,
    output_model=TaskWriteOutput,
    writes=True,
)
async def task_comment(inp: TaskCommentInput, ctx: ToolContext) -> TaskWriteOutput:
    run = await _run(ctx)
    if run and inp.task_id:
        await _ensure_run_task(ctx, inp.task_id, run)
    elif run and (
        inp.project_id is None or run.task_id is not None or run.project_id != inp.project_id
    ):
        raise Forbidden("Run hanya dapat memberi komentar project pada project cakupannya")
    if inp.task_id:
        task = await _scoped_task(ctx, inp.task_id, run)
        project_id = task.project_id
        entity_type, entity_id = "task", inp.task_id
    else:
        if inp.project_id is None:
            raise ValidationFailed("project_id wajib diisi")
        project_id = inp.project_id
        entity_type, entity_id = "project", project_id
        workspace_id = await _workspace_id(ctx, run)
        async with ctx.services.db.session() as session:
            project = await session.scalar(
                select(Project).where(
                    Project.id == project_id,
                    Project.workspace_id == workspace_id,
                    Project.deleted_at.is_(None),
                )
            )
        if project is None:
            raise NotFound("Project tidak ditemukan")
    if run:
        if ctx.services.outbox is None:
            raise ValidationFailed("Webhook outbox tidak dikonfigurasi")
        result = await ctx.services.outbox.emit_and_wait(
            str(run.run_id),
            "agent.comment.added",
            {
                "content": inp.content,
                "parent_comment_id": str(inp.parent_comment_id) if inp.parent_comment_id else None,
                "mentions": inp.mentions or [],
            },
            task_id=str(inp.task_id) if inp.task_id else None,
            use_run_task_id=False,
        )
    else:
        if ctx.services.service_api is None:
            raise ValidationFailed("Dashboard Service API tidak dikonfigurasi")
        result = await ctx.services.service_api.create_comment(
            _agent_id(ctx, None),
            {
                "project_id": str(project_id),
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "content": inp.content,
                "parent_comment_id": str(inp.parent_comment_id) if inp.parent_comment_id else None,
                "mentions": inp.mentions or [],
            },
        )
    return TaskWriteOutput(
        summary="Komentar ditambahkan",
        result=result,
        id=result.get("comment_id"),
    )


@tool(
    name="task_breakdown",
    toolset="task",
    summary="Pecah task menjadi subtask terjadwal",
    description="Rancang subtask dengan LLM, jadwalkan deterministik pada hari kerja, lalu "
    "preview atau buat maksimal 20 task.",
    input_model=BreakdownInput,
    output_model=BreakdownOutput,
    writes=True,
    timeout_s=90,
)
async def task_breakdown(inp: BreakdownInput, ctx: ToolContext) -> BreakdownOutput:
    run = await _run(ctx)
    task = await _scoped_task(ctx, inp.task_id, run)
    if run:
        await _ensure_run_task(ctx, inp.task_id, run)
    async with ctx.services.db.session() as session:
        blocks = list(
            (
                await session.scalars(
                    select(DocumentBlock)
                    .where(
                        DocumentBlock.task_id == task.id,
                        DocumentBlock.deleted_at.is_(None),
                    )
                    .order_by(DocumentBlock.order_index)
                )
            ).all()
        )
        existing = list(
            (
                await session.scalars(
                    select(Task).where(
                        Task.parent_task_id == task.id,
                        Task.deleted_at.is_(None),
                    )
                )
            ).all()
        )
    description = truncate(blocks_to_markdown(blocks), 2_000)
    prompt = render_prompt(
        "breakdown",
        title=task.title,
        description=description or "(tidak ada deskripsi)",
        existing="\n".join(f"- {item.title}" for item in existing) or "(belum ada)",
        max_subtasks=inp.max_subtasks,
        granularity=inp.granularity,
    )
    if ctx.services.llm is None:
        raise ValidationFailed("LLM Gateway tidak dikonfigurasi")
    route = (
        ctx.settings.llm.llm_route_fast
        if inp.max_subtasks <= 8
        else ctx.settings.llm.llm_route_smart
    )
    plan, usage = await ctx.services.llm.chat_json(
        "task_breakdown",
        [{"role": "user", "content": prompt}],
        BreakdownPlan,
        repair_attempts=1,
        budget=ctx.budget,
        prompt_version="v1",
        route_override=route,
    )
    ctx.llm_tokens += usage.total_tokens
    if len(plan.subtasks) > inp.max_subtasks:
        raise ValidationFailed(f"LLM menghasilkan lebih dari {inp.max_subtasks} subtask")
    start = task.start_date or today_in(ctx.settings.timezone)
    scheduled = schedule_subtasks(
        [item.model_dump(mode="json") for item in plan.subtasks],
        start_date=start.isoformat(),
        parent_end_date=task.end_date.isoformat() if task.end_date else None,
    )
    total_days = sum(item["duration_days"] for item in scheduled)
    outside = any(item["outside_parent_range"] for item in scheduled)
    if inp.mode == "preview":
        return BreakdownOutput(
            summary=f"Preview {len(scheduled)} subtask, total {total_days} hari kerja",
            subtasks=scheduled,
            total_duration_days=total_days,
            outside_parent_range=outside,
        )
    created = await _create_tasks(
        ctx,
        project_id=task.project_id,
        parent_task_id=task.id,
        tasks=[
            {
                "client_ref": item["client_ref"],
                "title": item["title"],
                "priority": "medium",
                "start_date": item["start_date"],
                "end_date": item["end_date"],
                "tags": item.get("tags", []),
                "initial_blocks": [
                    {
                        "block_type": "checkListItem",
                        "content_json": validate_block_content(
                            "checkListItem",
                            {"content": [{"type": "text", "text": text, "styles": {}}]},
                        ),
                    }
                    for text in item.get("checklist", [])
                ],
            }
            for item in scheduled
        ],
        run=run,
    )
    return BreakdownOutput(
        summary=f"{len(created)} subtask dibuat",
        subtasks=scheduled,
        total_duration_days=total_days,
        outside_parent_range=outside,
        created=created,
    )
