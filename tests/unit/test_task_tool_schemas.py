from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mcp_marketing.core.errors import Forbidden
from mcp_marketing.services.templates.renderer import RenderedTemplate
from mcp_marketing.tools import task as task_tools
from mcp_marketing.tools import tpl as template_tools
from mcp_marketing.tools.task import (
    CreateItem,
    TaskCommentInput,
    TaskMoveInput,
    TaskUpdateInput,
)
from mcp_marketing.tools.tpl import TemplateTarget


def test_task_update_rejects_assignee_and_invalid_milestone() -> None:
    with pytest.raises(ValidationError, match="assignee hanya bisa diubah manusia"):
        TaskUpdateInput.model_validate(
            {"task_id": "00000000-0000-0000-0000-000000000001", "assignee": "x"}
        )
    with pytest.raises(ValidationError, match="Milestone"):
        CreateItem.model_validate({"title": "Milestone", "is_milestone": True})


def test_task_create_and_comment_normalize_user_text() -> None:
    task = CreateItem.model_validate(
        {
            "title": "  Brief  ",
            "start_date": "2026-11-02",
            "end_date": "2026-11-02",
            "is_milestone": True,
        }
    )
    comment = TaskCommentInput.model_validate(
        {"task_id": "00000000-0000-0000-0000-000000000001", "content": "  Selesai  "}
    )

    assert task.title == "Brief"
    assert task.start_date == date(2026, 11, 2)
    assert comment.content == "Selesai"


@pytest.mark.asyncio
async def test_task_move_run_cannot_reorder_outside_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    run_task_id = uuid4()
    outside_task_id = uuid4()
    run = SimpleNamespace(run_id=uuid4(), task_id=run_task_id, agent_id=uuid4())
    service_api = SimpleNamespace(move_task=AsyncMock())
    ctx = SimpleNamespace(services=SimpleNamespace(service_api=service_api))

    async def get_run(_ctx: object) -> SimpleNamespace:
        return run

    async def ensure_run_task(
        _ctx: object, task_id: object, _run: SimpleNamespace
    ) -> SimpleNamespace:
        if task_id != run_task_id:
            raise Forbidden("Task berada di luar cakupan run")
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(task_tools, "_run", get_run)
    monkeypatch.setattr(task_tools, "_ensure_run_task", ensure_run_task)

    with pytest.raises(Forbidden, match="di luar cakupan"):
        await task_tools.task_move(
            TaskMoveInput(task_id=run_task_id, before_task_id=outside_task_id), ctx
        )

    service_api.move_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_move_run_cannot_detach_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    run_task_id = uuid4()
    child_task_id = uuid4()
    run = SimpleNamespace(run_id=uuid4(), task_id=run_task_id, agent_id=uuid4())
    service_api = SimpleNamespace(move_task=AsyncMock())
    ctx = SimpleNamespace(services=SimpleNamespace(service_api=service_api))

    async def get_run(_ctx: object) -> SimpleNamespace:
        return run

    async def ensure_run_task(
        _ctx: object, task_id: object, _run: SimpleNamespace
    ) -> SimpleNamespace:
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(task_tools, "_run", get_run)
    monkeypatch.setattr(task_tools, "_ensure_run_task", ensure_run_task)

    with pytest.raises(Forbidden, match="keluar dari cakupan run"):
        await task_tools.task_move(TaskMoveInput(task_id=child_task_id), ctx)

    service_api.move_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_comment_allows_project_run_comment_with_null_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    workspace_id = uuid4()
    run = SimpleNamespace(
        run_id=uuid4(), task_id=None, project_id=project_id, workspace_id=workspace_id
    )

    class SessionContext:
        async def __aenter__(self) -> SessionContext:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _query: object) -> object:
            return object()

    outbox = SimpleNamespace(emit_and_wait=AsyncMock(return_value={"comment_id": "c1"}))
    ctx = SimpleNamespace(
        services=SimpleNamespace(
            outbox=outbox,
            db=SimpleNamespace(session=lambda: SessionContext()),
        )
    )

    async def get_run(_ctx: object) -> SimpleNamespace:
        return run

    async def get_workspace(_ctx: object, _run: object) -> object:
        return workspace_id

    monkeypatch.setattr(task_tools, "_run", get_run)
    monkeypatch.setattr(task_tools, "_workspace_id", get_workspace)

    result = await task_tools.task_comment(
        TaskCommentInput(project_id=project_id, content="Catatan project"), ctx
    )

    assert result.id == "c1"
    assert outbox.emit_and_wait.await_args.kwargs["task_id"] is None
    assert outbox.emit_and_wait.await_args.kwargs["use_run_task_id"] is False


@pytest.mark.asyncio
async def test_task_scoped_run_cannot_comment_on_project(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(run_id=uuid4(), task_id=uuid4(), project_id=uuid4())
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=run))

    with pytest.raises(Forbidden, match="komentar project"):
        await task_tools.task_comment(
            TaskCommentInput(project_id=run.project_id, content="Catatan project"),
            SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_project_brief_template_from_run_does_not_partially_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_project = AsyncMock()
    monkeypatch.setattr(template_tools, "_check_project", check_project)
    run = SimpleNamespace(task_id=None)
    ctx = SimpleNamespace()
    target = TemplateTarget(type="project_brief", project_id=uuid4())
    rendered = RenderedTemplate(
        blocks=[],
        subtasks=[
            {
                "client_ref": "root",
                "parent_client_ref": None,
                "title": "Task root",
                "start_date": None,
                "end_date": None,
                "tags": [],
                "initial_blocks": [],
            }
        ],
        outline={},
    )

    with pytest.raises(Forbidden, match="Project Brief"):
        await template_tools._apply(ctx, target, rendered, run)

    check_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_scoped_run_cannot_apply_project_brief_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(template_tools, "_check_project", AsyncMock())
    monkeypatch.setattr(
        template_tools,
        "chain_append_ops",
        lambda *_args, **_kwargs: [{"type": "insert"}],
    )
    monkeypatch.setattr(
        template_tools,
        "chunk_block_ops",
        lambda operations: [operations],
    )
    outbox = SimpleNamespace(emit_and_wait=AsyncMock())
    run = SimpleNamespace(task_id=uuid4(), run_id=uuid4())
    ctx = SimpleNamespace(services=SimpleNamespace(outbox=outbox))
    target = TemplateTarget(type="project_brief", project_id=uuid4())
    rendered = RenderedTemplate(
        blocks=[SimpleNamespace()],
        subtasks=[],
        outline={},
    )

    with pytest.raises(Forbidden, match="Project Brief"):
        await template_tools._apply(ctx, target, rendered, run)

    outbox.emit_and_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_scoped_run_can_apply_project_brief_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(template_tools, "_check_project", AsyncMock())
    monkeypatch.setattr(
        template_tools,
        "chain_append_ops",
        lambda *_args, **_kwargs: [{"type": "insert"}],
    )
    monkeypatch.setattr(
        template_tools,
        "chunk_block_ops",
        lambda operations: [operations],
    )
    outbox = SimpleNamespace(emit_and_wait=AsyncMock())
    run = SimpleNamespace(task_id=None, run_id=uuid4())
    ctx = SimpleNamespace(services=SimpleNamespace(outbox=outbox))
    target = TemplateTarget(type="project_brief", project_id=uuid4())
    rendered = RenderedTemplate(
        blocks=[SimpleNamespace()],
        subtasks=[],
        outline={},
    )

    assert await template_tools._apply(ctx, target, rendered, run) == (1, [])
    assert outbox.emit_and_wait.await_args.kwargs["task_id"] is None
    assert outbox.emit_and_wait.await_args.kwargs["use_run_task_id"] is False
