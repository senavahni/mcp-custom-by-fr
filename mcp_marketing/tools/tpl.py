"""Template library, sandboxed rendering, and Dashboard application tools."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from mcp_marketing.core.errors import Forbidden, NotFound, ValidationFailed
from mcp_marketing.models.dashboard import Project, Task
from mcp_marketing.schemas.blocks import BLOCK_TYPES
from mcp_marketing.schemas.common import ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.dashboard.chunking import chain_append_ops, chunk_block_ops
from mcp_marketing.services.llm.prompts import render as render_prompt
from mcp_marketing.services.templates.library import (
    get_template,
    list_templates,
    save_template,
)
from mcp_marketing.services.templates.renderer import RenderedTemplate, render
from mcp_marketing.utils.time import today_in
from mcp_marketing.utils.tokens import truncate_to_tokens

TemplateCategory = Literal[
    "campaign",
    "content_calendar",
    "market_research",
    "launch",
    "social_listening",
    "competitor",
    "report",
    "custom",
]


class TemplateListInput(ToolInput):
    category: TemplateCategory | None = None


class TemplateListOutput(BaseModel):
    summary: str
    cols: list[str]
    rows: list[list[Any]]


class TemplateGetInput(ToolInput):
    template_key: str = Field(min_length=1, max_length=64)
    format: Literal["outline", "full"] = "outline"


class TemplateGetOutput(BaseModel):
    summary: str
    template: dict[str, Any]


class TemplateTarget(ToolInput):
    type: Literal["project_brief", "task", "new_task"]
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def target_shape(self) -> TemplateTarget:
        if self.type == "task" and self.task_id is None:
            raise ValueError("target task memerlukan task_id")
        if self.type == "project_brief" and (self.task_id or self.parent_task_id):
            raise ValueError("target project_brief tidak menerima task_id")
        if self.type == "new_task" and self.title is None:
            raise ValueError("target new_task memerlukan title")
        return self


class TemplateRenderInput(ToolInput):
    template_key: str = Field(min_length=1, max_length=64)
    variables: dict[str, Any] = Field(default_factory=dict)
    target: TemplateTarget
    mode: Literal["preview", "apply"] = "preview"


class TemplateSaveInput(ToolInput):
    template: dict[str, Any]


class TemplateGenerateInput(ToolInput):
    brief: str = Field(min_length=1, max_length=4_000)
    kind: TemplateCategory
    target: TemplateTarget | None = None
    mode: Literal["preview", "apply", "save"] = "preview"
    language: str = Field(default="Bahasa Indonesia", min_length=2, max_length=40)

    @model_validator(mode="after")
    def requires_target_for_apply(self) -> TemplateGenerateInput:
        if self.mode == "apply" and self.target is None:
            raise ValueError("mode apply memerlukan target")
        return self


class TemplateOutput(BaseModel):
    summary: str
    key: str | None = None
    version: int | None = None
    outline: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    blocks_applied: int = 0
    subtasks_created: list[dict[str, Any]] = Field(default_factory=list)
    target_url: str | None = None
    template: dict[str, Any] | None = None


def _workspace_id(ctx: ToolContext, run: Any) -> uuid.UUID:
    workspace_id = str(run.workspace_id) if run and run.workspace_id else ctx.client.workspace_id
    if not workspace_id:
        raise Forbidden("Klien MCP belum dibatasi ke workspace Dashboard")
    try:
        return uuid.UUID(workspace_id)
    except ValueError:
        raise Forbidden("Workspace klien tidak valid") from None


async def _run(ctx: ToolContext) -> Any:
    if not ctx.run_id:
        return None
    from mcp_marketing.models import Run

    async with ctx.services.db.session() as session:
        run = await session.get(Run, uuid.UUID(ctx.run_id))
    if run is None:
        raise Forbidden("Run tidak dikenal")
    return run


async def _target_depth(ctx: ToolContext, target: TemplateTarget, run: Any) -> int:
    workspace = _workspace_id(ctx, run)
    if run and str(run.project_id) != str(target.project_id):
        raise NotFound("Project tidak ditemukan")
    if target.type == "project_brief":
        return 0
    task_id = target.task_id if target.type == "task" else target.parent_task_id
    if task_id is None:
        return 1 if target.type == "new_task" else 0
    async with ctx.services.db.session() as session:
        task = await session.scalar(
            select(Task)
            .join(Project, Project.id == Task.project_id)
            .where(
                Task.id == task_id,
                Task.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                Project.workspace_id == workspace,
                Project.id == target.project_id,
            )
        )
    if task is None:
        raise NotFound("Task target tidak ditemukan")
    if run:
        from mcp_marketing.tools.task import _ensure_run_task

        await _ensure_run_task(ctx, task.id, run)
    if target.type == "new_task":
        if task.depth >= 3:
            raise ValidationFailed("Template melebihi kedalaman Dashboard maksimum (3)")
        return task.depth + 1
    return task.depth


def _sample_variables(template: dict[str, Any]) -> dict[str, Any]:
    samples: dict[str, Any] = {}
    for spec in template.get("variables", []):
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            raise ValidationFailed("Definisi variable template tidak valid")
        if "default" in spec:
            samples[spec["name"]] = spec["default"]
        elif spec.get("required"):
            kind = str(spec.get("type", "string"))
            samples[spec["name"]] = {
                "date": "2026-01-05",
                "list": ["contoh"],
            }.get(kind, "Contoh")
    return samples


def _validate_template(template: dict[str, Any]) -> RenderedTemplate:
    required = ("key", "name", "blocks", "subtasks")
    if any(field not in template for field in required):
        raise ValidationFailed("Template wajib memiliki key, name, blocks, dan subtasks")
    if not isinstance(template["name"], str) or not template["name"].strip():
        raise ValidationFailed("Nama template wajib diisi")
    if not isinstance(template["blocks"], dict) or not isinstance(
        template["blocks"].get("md"), str
    ):
        raise ValidationFailed("Template blocks.md wajib berupa markdown")
    if not isinstance(template["subtasks"], list):
        raise ValidationFailed("Template subtasks harus berupa daftar")
    return render(
        template,
        _sample_variables(template),
        date(2026, 1, 5),
    )


async def _check_project(ctx: ToolContext, project_id: uuid.UUID, run: Any) -> None:
    workspace = _workspace_id(ctx, run)
    if run and str(run.project_id) != str(project_id):
        raise NotFound("Project tidak ditemukan")
    async with ctx.services.db.session() as session:
        project = await session.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == workspace,
                Project.deleted_at.is_(None),
            )
        )
    if project is None:
        raise NotFound("Project tidak ditemukan")


async def _apply(
    ctx: ToolContext,
    target: TemplateTarget,
    rendered: RenderedTemplate,
    run: Any,
) -> tuple[int, list[dict[str, Any]]]:
    await _check_project(ctx, target.project_id, run)
    if target.type == "project_brief" and run is not None and run.task_id is not None:
        raise Forbidden("Run task-scoped tidak dapat menerapkan template ke Project Brief")
    if target.type == "project_brief" and run is not None and rendered.subtasks:
        raise Forbidden(
            "Run hanya dapat membuat task dalam cakupannya; target Project Brief "
            "tidak dapat membuat root task"
        )
    task_id = target.task_id if target.type == "task" else None
    if target.type == "new_task":
        from mcp_marketing.tools.task import _create_tasks

        created_root = await _create_tasks(
            ctx,
            project_id=target.project_id,
            parent_task_id=target.parent_task_id,
            tasks=[
                {
                    "client_ref": "template-root",
                    "title": target.title,
                    "initial_blocks": [],
                }
            ],
            run=run,
        )
        if not created_root or not created_root[0].get("id"):
            raise ValidationFailed("Dashboard tidak mengembalikan id task baru")
        task_id = uuid.UUID(str(created_root[0]["id"]))

    if rendered.blocks:
        ops = chain_append_ops(rendered.blocks, after_block_id=None)
        if run:
            if ctx.services.outbox is None:
                raise ValidationFailed("Webhook outbox tidak dikonfigurasi")
            for chunk in chunk_block_ops(ops):
                await ctx.services.outbox.emit_and_wait(
                    str(run.run_id),
                    "agent.block.appended",
                    {"operations": chunk},
                    task_id=str(task_id) if task_id else None,
                    use_run_task_id=task_id is not None,
                )
        else:
            if ctx.services.service_api is None or not ctx.client.dashboard_agent_id:
                raise Forbidden("Dashboard Service API belum dikonfigurasi untuk klien")
            for chunk in chunk_block_ops(ops):
                await ctx.services.service_api.apply_block_operations(
                    ctx.client.dashboard_agent_id,
                    {
                        "target": {
                            "project_id": str(target.project_id),
                            "task_id": str(task_id) if task_id else None,
                        },
                        "operations": chunk,
                    },
                )

    created: list[dict[str, Any]] = []
    if rendered.subtasks:
        from mcp_marketing.tools.task import _create_tasks

        specs = {
            str(item["client_ref"]): {
                key: item[key]
                for key in (
                    "client_ref",
                    "title",
                    "start_date",
                    "end_date",
                    "tags",
                    "initial_blocks",
                )
            }
            for item in rendered.subtasks
        }
        refs: dict[str, str] = {}
        root_items = [item for item in rendered.subtasks if item["parent_client_ref"] is None]
        if target.type == "project_brief":
            api = ctx.services.service_api
            agent_id = ctx.client.dashboard_agent_id
            if api is None or not agent_id:
                raise Forbidden("Dashboard Service API belum dikonfigurasi untuk membuat task")
            result = await api.create_tasks(
                agent_id,
                str(target.project_id),
                [specs[item["client_ref"]] for item in root_items],
            )
            batch = result.get("created") or []
        else:
            parent_id = task_id
            if parent_id is None:
                raise ValidationFailed("Target template belum memiliki task parent")
            batch = await _create_tasks(
                ctx,
                project_id=target.project_id,
                parent_task_id=uuid.UUID(str(parent_id)),
                tasks=[specs[item["client_ref"]] for item in root_items],
                run=run,
            )
        created.extend(batch)
        refs.update(
            {
                str(item["client_ref"]): str(item["id"])
                for item in batch
                if item.get("client_ref") and item.get("id")
            }
        )
        pending = [item for item in rendered.subtasks if item["parent_client_ref"] is not None]
        while pending:
            ready = [item for item in pending if item["parent_client_ref"] in refs]
            if not ready:
                raise ValidationFailed("Hierarki subtask template tidak valid")
            for item in ready:
                parent_task_ref = refs[str(item["parent_client_ref"])]
                if target.type == "project_brief":
                    agent_id = str(run.agent_id) if run else ctx.client.dashboard_agent_id
                    api = ctx.services.service_api
                    if api is None or agent_id is None:
                        raise Forbidden(
                            "Dashboard Service API belum dikonfigurasi untuk membuat subtask"
                        )
                    child_result = await api.create_subtasks(
                        agent_id, parent_task_ref, [specs[item["client_ref"]]]
                    )
                    child_created = child_result.get("created") or []
                else:
                    child_created = await _create_tasks(
                        ctx,
                        project_id=target.project_id,
                        parent_task_id=uuid.UUID(parent_task_ref),
                        tasks=[specs[item["client_ref"]]],
                        run=run,
                    )
                created.extend(child_created)
                if child_created and child_created[0].get("id"):
                    refs[str(item["client_ref"])] = str(child_created[0]["id"])
                pending.remove(item)
    return len(rendered.blocks), created


@tool(
    name="tpl_list",
    toolset="tpl",
    summary="Daftar template dokumen",
    description="Daftar ringkas template aktif (cache 300 detik), difilter kategori dan "
    "workspace klien.",
    input_model=TemplateListInput,
    output_model=TemplateListOutput,
    cache_ttl_s=300,
)
async def tpl_list(inp: TemplateListInput, ctx: ToolContext) -> TemplateListOutput:
    async with ctx.services.db.session() as session:
        rows = await list_templates(session, inp.category, ctx.client.workspace_id, ctx.client.id)
    return TemplateListOutput(
        summary=f"{len(rows)} template tersedia",
        cols=["key", "name", "category", "origin", "version"],
        rows=[
            [row["key"], row["name"], row["category"], row["origin"], row["current_version"]]
            for row in rows
        ],
    )


@tool(
    name="tpl_get",
    toolset="tpl",
    summary="Ambil outline atau isi template",
    description="Ambil outline hemat token (default) atau markdown dan definisi subtask; tidak "
    "mengembalikan JSON blok.",
    input_model=TemplateGetInput,
    output_model=TemplateGetOutput,
)
async def tpl_get(inp: TemplateGetInput, ctx: ToolContext) -> TemplateGetOutput:
    async with ctx.services.db.session() as session:
        template = await get_template(
            session, inp.template_key, ctx.client.workspace_id, ctx.client.id
        )
    if inp.format == "outline":
        result = {
            key: template[key]
            for key in ("key", "name", "category", "description", "outline", "version")
        }
    else:
        result = {
            key: template[key]
            for key in (
                "key",
                "name",
                "category",
                "description",
                "variables",
                "subtasks",
                "version",
            )
        }
        result["markdown"] = template["blocks"].get("md", "")
    return TemplateGetOutput(summary=f"Template {template['name']}", template=result)


@tool(
    name="tpl_render",
    toolset="tpl",
    summary="Preview atau terapkan template ke Dashboard",
    description="Render markdown template dalam sandbox dan validasi semua blok/kedalaman "
    "sebelum preview atau apply ke Project Brief, task, atau task baru.",
    input_model=TemplateRenderInput,
    output_model=TemplateOutput,
    writes=True,
    timeout_s=90,
)
async def tpl_render(inp: TemplateRenderInput, ctx: ToolContext) -> TemplateOutput:
    run = await _run(ctx)
    target_depth = await _target_depth(ctx, inp.target, run)
    async with ctx.services.db.session() as session:
        template = await get_template(
            session, inp.template_key, ctx.client.workspace_id, ctx.client.id
        )
    try:
        base = date.fromisoformat(
            str(inp.variables.get("start_date") or today_in(ctx.settings.timezone))
        )
    except ValueError:
        raise ValidationFailed("start_date harus berformat YYYY-MM-DD") from None
    rendered = render(template, inp.variables, base, target_depth=target_depth)
    if inp.mode == "preview":
        return TemplateOutput(
            summary=f"Preview {len(rendered.blocks)} blok dan {len(rendered.subtasks)} subtask",
            key=inp.template_key,
            outline=rendered.outline,
        )
    blocks_applied, created = await _apply(ctx, inp.target, rendered, run)
    return TemplateOutput(
        summary=f"{blocks_applied} blok diterapkan; {len(created)} subtask dibuat",
        key=inp.template_key,
        outline=rendered.outline,
        blocks_applied=blocks_applied,
        subtasks_created=created,
    )


@tool(
    name="tpl_save",
    toolset="tpl",
    summary="Simpan versi baru template",
    description="Validasi markdown, variabel, blok, dan subtask sebelum menyimpan versi baru "
    "template privat klien/workspace.",
    input_model=TemplateSaveInput,
    output_model=TemplateOutput,
    writes=True,
)
async def tpl_save(inp: TemplateSaveInput, ctx: ToolContext) -> TemplateOutput:
    template = dict(inp.template)
    template["outline"] = _validate_template(template).outline
    async with ctx.services.db.session() as session:
        version = await save_template(
            session,
            template,
            client_id=ctx.client.id,
            workspace_id=ctx.client.workspace_id,
            origin="custom",
        )
        await session.commit()
    return TemplateOutput(
        summary=f"Template {inp.template['key']} tersimpan",
        key=template["key"],
        version=version,
    )


class _GeneratedSubtask(BaseModel):
    client_ref: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    offset_days: int = Field(default=0, ge=0, le=365)
    duration_days: int = Field(default=1, ge=1, le=60)
    tags: list[str] = Field(default_factory=list, max_length=20)
    md: str = ""
    children: list[_GeneratedSubtask] = Field(default_factory=list, max_length=20)


class _GeneratedTemplate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    variables: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    md: str = Field(min_length=1, max_length=20_000)
    subtasks: list[_GeneratedSubtask] = Field(default_factory=list, max_length=20)


@tool(
    name="tpl_generate",
    toolset="tpl",
    summary="Generate template dari brief",
    description="Buat template markdown terstruktur dari brief (maks 4.000 karakter), validasi "
    "hasil sandbox, dan preview/apply/simpan.",
    input_model=TemplateGenerateInput,
    output_model=TemplateOutput,
    writes=True,
    timeout_s=120,
)
async def tpl_generate(inp: TemplateGenerateInput, ctx: ToolContext) -> TemplateOutput:
    if ctx.services.llm is None:
        raise ValidationFailed("LLM Gateway tidak dikonfigurasi")
    run = await _run(ctx)
    workspace_id = ctx.client.workspace_id
    async with ctx.services.db.session() as session:
        examples = await list_templates(session, inp.kind, workspace_id, ctx.client.id)
        sample = None
        if examples:
            sample = await get_template(session, examples[0]["key"], workspace_id, ctx.client.id)
    sample_outline = str(sample["outline"]) if sample else "(tidak ada contoh)"
    prompt = render_prompt(
        "tpl_generate",
        brief=inp.brief,
        kind=inp.kind,
        language=inp.language,
        block_types=", ".join(BLOCK_TYPES),
        example=truncate_to_tokens(sample_outline, 300),
    )
    generated: _GeneratedTemplate | None = None
    candidate: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(3):
        if attempt:
            prompt = (
                f"{prompt}\n\nPerbaiki hasil sebelumnya agar lolos validasi. "
                f"Kesalahan: {last_error}\nHasil sebelumnya: {candidate}"
            )
        generated, usage = await ctx.services.llm.chat_json(
            "tpl_generate",
            [{"role": "user", "content": prompt}],
            _GeneratedTemplate,
            repair_attempts=1,
            budget=ctx.budget,
            prompt_version="v1",
            route_override=ctx.settings.llm.llm_route_smart,
        )
        ctx.llm_tokens += usage.total_tokens
        key = f"generated-{uuid.uuid4().hex[:12]}"
        candidate = {
            "key": key,
            "name": generated.name,
            "category": inp.kind,
            "variables": generated.variables,
            "blocks": {"md": generated.md},
            "subtasks": [item.model_dump(mode="json") for item in generated.subtasks],
        }
        try:
            candidate["outline"] = _validate_template(candidate).outline
            break
        except ValidationFailed as exc:
            last_error = exc.message
    else:
        raise ValidationFailed(
            f"Template hasil generate tidak valid setelah 2 perbaikan: {last_error}"
        )

    assert candidate is not None
    outline = _validate_template(candidate).outline
    if inp.mode == "save":
        async with ctx.services.db.session() as session:
            version = await save_template(
                session,
                candidate,
                client_id=ctx.client.id,
                workspace_id=workspace_id,
                origin="generated",
            )
            await session.commit()
        return TemplateOutput(
            summary="Template hasil generate disimpan",
            key=candidate["key"],
            version=version,
            outline=outline,
        )
    if inp.mode == "preview":
        return TemplateOutput(
            summary="Template hasil generate tervalidasi",
            key=candidate["key"],
            outline=outline,
            template=candidate,
        )
    if inp.target is None:
        raise ValidationFailed("mode apply memerlukan target")
    depth = await _target_depth(ctx, inp.target, run)
    rendered = render(
        candidate,
        _sample_variables(candidate),
        date.fromisoformat(
            str(_sample_variables(candidate).get("start_date") or today_in(ctx.settings.timezone))
        ),
        target_depth=depth,
    )
    blocks_applied, created = await _apply(ctx, inp.target, rendered, run)
    return TemplateOutput(
        summary=f"Template diterapkan: {blocks_applied} blok, {len(created)} subtask",
        key=candidate["key"],
        outline=rendered.outline,
        blocks_applied=blocks_applied,
        subtasks_created=created,
    )
