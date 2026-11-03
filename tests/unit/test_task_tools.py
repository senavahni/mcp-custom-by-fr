from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from mcp_marketing.core.errors import Forbidden, HierarchyDepthExceeded, NotFound, ValidationFailed
from mcp_marketing.tools import task as task_tools
from mcp_marketing.tools.task import (
    BreakdownItem,
    BreakdownPlan,
    TaskCreateInput,
    TaskGetInput,
    TaskListInput,
    TaskRescheduleInput,
    TaskUpdateInput,
)


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _Session:
    def __init__(
        self,
        *,
        scalar_values: list[object] | None = None,
        executed_rows: list[object] | None = None,
        scalar_rows: list[list[object]] | None = None,
        get_value: object = None,
    ) -> None:
        self.scalar_values = list(scalar_values or [])
        self.executed_rows = executed_rows or []
        self.scalar_rows = list(scalar_rows or [])
        self.get_value = get_value

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: object) -> object:
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def execute(self, _query: object) -> _Rows:
        return _Rows(self.executed_rows)

    async def scalars(self, _query: object) -> _Rows:
        rows = self.scalar_rows.pop(0) if self.scalar_rows else []
        return _Rows(rows)

    async def get(self, _model: object, _identity: object) -> object:
        return self.get_value


def _ctx(session: _Session | None = None, **service_values: object) -> SimpleNamespace:
    session = session or _Session()
    services_data: dict[str, object] = {
        "db": SimpleNamespace(session=lambda: session),
        "service_api": None,
        "outbox": None,
        "llm": None,
    }
    services_data.update(service_values)
    services = SimpleNamespace(**services_data)
    return SimpleNamespace(
        run_id=None,
        client=SimpleNamespace(workspace_id=str(uuid4()), dashboard_agent_id="agent-1"),
        services=services,
        settings=SimpleNamespace(
            timezone="UTC",
            llm=SimpleNamespace(llm_route_fast="fast", llm_route_smart="smart"),
        ),
        budget=None,
        llm_tokens=0,
    )


@pytest.mark.asyncio
async def test_run_workspace_and_task_scope_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    run = SimpleNamespace(task_id=uuid4(), agent_id=uuid4(), workspace_id=uuid4())
    context = _ctx(_Session(get_value=run))
    context.run_id = str(run_id)

    assert await task_tools._run(context) is run
    assert await task_tools._workspace_id(context, run) == run.workspace_id
    assert task_tools._agent_id(context, run) == str(run.agent_id)
    assert task_tools._agent_id(_ctx(), None) == "agent-1"

    missing_run = _ctx(_Session(get_value=None))
    missing_run.run_id = str(run_id)
    with pytest.raises(Forbidden, match="Run tidak dikenal"):
        await task_tools._run(missing_run)

    no_workspace = SimpleNamespace(client=SimpleNamespace(workspace_id=None))
    with pytest.raises(Forbidden, match="workspace"):
        await task_tools._workspace_id(no_workspace, None)
    invalid_workspace = SimpleNamespace(client=SimpleNamespace(workspace_id="invalid"))
    with pytest.raises(Forbidden, match="tidak valid"):
        await task_tools._workspace_id(invalid_workspace, None)

    task = SimpleNamespace(id=uuid4(), parent_task_id=run.task_id)
    looked_up: list[object] = []

    async def scoped(_ctx: object, task_id: object, _run: object = None) -> object:
        looked_up.append(task_id)
        if task_id == run.task_id:
            return SimpleNamespace(id=run.task_id, parent_task_id=None)
        return task

    original_scoped_task = task_tools._scoped_task
    monkeypatch.setattr(task_tools, "_scoped_task", scoped)
    assert await task_tools._ensure_run_task(context, task.id, run) is task
    assert looked_up == [task.id, run.task_id]

    with pytest.raises(Forbidden, match="Run ini tidak memiliki task"):
        await task_tools._ensure_run_task(context, task.id, SimpleNamespace(task_id=None))

    with pytest.raises(Forbidden, match="di luar cakupan"):
        await task_tools._ensure_run_task(
            context,
            task.id,
            SimpleNamespace(task_id=uuid4()),
        )

    monkeypatch.setattr(task_tools, "_scoped_task", original_scoped_task)
    with pytest.raises(NotFound, match="Task tidak ditemukan"):
        await task_tools._scoped_task(_ctx(_Session(scalar_values=[None])), uuid4())


@pytest.mark.asyncio
async def test_create_tasks_uses_run_outbox_and_service_api_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, parent_id = uuid4(), uuid4()
    run = SimpleNamespace(task_id=parent_id, agent_id=uuid4(), run_id=uuid4())
    parent = SimpleNamespace(project_id=project_id, depth=1)
    monkeypatch.setattr(
        task_tools,
        "_ensure_run_task",
        AsyncMock(return_value=parent),
    )
    outbox = SimpleNamespace(emit_and_wait=AsyncMock(return_value={"created": [{"id": "child"}]}))
    context = _ctx(outbox=outbox)
    task_data = [{"title": "Child", "is_milestone": False}]

    created = await task_tools._create_tasks(
        context,
        project_id=project_id,
        parent_task_id=None,
        tasks=task_data,
        run=run,
    )

    assert created == [{"id": "child"}]
    sent = outbox.emit_and_wait.await_args.args[2]["subtasks"]
    assert sent == [{"title": "Child"}]

    service_api = SimpleNamespace(
        create_tasks=AsyncMock(return_value={"created": [{"id": "root"}]}),
        create_subtasks=AsyncMock(return_value={"created": [{"id": "nested"}]}),
    )
    external = _ctx(service_api=service_api)
    assert await task_tools._create_tasks(
        external,
        project_id=project_id,
        parent_task_id=None,
        tasks=task_data,
        run=None,
    ) == [{"id": "root"}]
    assert await task_tools._create_tasks(
        external,
        project_id=project_id,
        parent_task_id=parent_id,
        tasks=task_data,
        run=None,
    ) == [{"id": "nested"}]

    milestone_api = SimpleNamespace(
        create_subtasks=AsyncMock(return_value={"created": [{"id": "milestone"}]})
    )
    milestone_context = _ctx(service_api=milestone_api)
    assert await task_tools._create_tasks(
        milestone_context,
        project_id=project_id,
        parent_task_id=parent_id,
        tasks=[{"title": "Milestone", "is_milestone": True}],
        run=run,
    ) == [{"id": "milestone"}]

    with pytest.raises(Forbidden, match="Run tanpa task"):
        await task_tools._create_tasks(
            context,
            project_id=project_id,
            parent_task_id=None,
            tasks=task_data,
            run=SimpleNamespace(task_id=None),
        )
    with pytest.raises(ValidationFailed, match="Project task"):
        await task_tools._create_tasks(
            context,
            project_id=uuid4(),
            parent_task_id=parent_id,
            tasks=task_data,
            run=run,
        )
    monkeypatch.setattr(
        task_tools,
        "_ensure_run_task",
        AsyncMock(return_value=SimpleNamespace(project_id=project_id, depth=3)),
    )
    with pytest.raises(HierarchyDepthExceeded):
        await task_tools._create_tasks(
            context,
            project_id=project_id,
            parent_task_id=parent_id,
            tasks=task_data,
            run=run,
        )


@pytest.mark.asyncio
async def test_task_list_applies_filters_and_encodes_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    task = SimpleNamespace(
        id=uuid4(),
        title="Campaign",
        status="todo",
        priority="high",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 6),
        depth=1,
        tags=["launch"],
        assignee_type="agent",
    )
    session = _Session(
        scalar_values=[3],
        executed_rows=[(task, "agent-x"), (task, "agent-x")],
    )
    context = _ctx(session)
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=None))
    monkeypatch.setattr(task_tools, "_workspace_id", AsyncMock(return_value=uuid4()))

    output = await task_tools.task_list(
        TaskListInput(
            project_id=project_id,
            status=["todo"],
            parent_task_id=uuid4(),
            due_before=date(2026, 11, 10),
            tag="launch",
            assignee="agent",
            fields=["id", "start", "end", "assignee"],
            limit=1,
            cursor=task_tools._encode_cursor(5),
        ),
        context,
    )

    assert output.total == 3
    assert output.rows == [[str(task.id), "2026-11-02", "2026-11-06", "agent-x"]]
    assert task_tools._cursor_offset(output.next_cursor) == 6

    monkeypatch.setattr(task_tools, "_workspace_id", AsyncMock(return_value=uuid4()))
    no_assignee = _ctx(_Session(scalar_values=[0], executed_rows=[]))
    output = await task_tools.task_list(
        TaskListInput(project_id=project_id, assignee="none", fields=["title"]),
        no_assignee,
    )
    assert output.rows == []
    assert output.next_cursor is None


def test_task_cursor_validation() -> None:
    assert task_tools._cursor_offset(None) == 0
    assert task_tools._cursor_offset(task_tools._encode_cursor(12)) == 12
    with pytest.raises(ValidationFailed, match="Cursor task"):
        task_tools._cursor_offset("not-base64")
    with pytest.raises(ValidationFailed, match="Cursor task"):
        task_tools._cursor_offset(task_tools._encode_cursor(-1))


@pytest.mark.asyncio
async def test_task_get_returns_blocks_subtasks_and_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, project_id = uuid4(), uuid4()
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        parent_task_id=None,
        depth=0,
        title="Campaign",
        status="todo",
        priority="medium",
        start_date=date(2026, 11, 2),
        end_date=None,
        is_milestone=False,
        tags=None,
    )
    child = SimpleNamespace(id=uuid4(), title="Week 1", status="todo", depth=1)
    grandchild = SimpleNamespace(id=uuid4(), title="Draft", status="done", depth=2)
    block = SimpleNamespace(block_type="paragraph", content_json={})
    comment = SimpleNamespace(
        id=uuid4(),
        content="Comment",
        author_type="agent",
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    session = _Session(scalar_rows=[[block], [child], [grandchild], [comment]])
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=None))
    monkeypatch.setattr(task_tools, "_scoped_task", AsyncMock(return_value=task))
    monkeypatch.setattr(task_tools, "_workspace_id", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(task_tools, "block_to_markdown", lambda *_args: "Paragraph")
    monkeypatch.setattr(task_tools, "blocks_to_markdown", lambda _blocks: "## Brief")

    output = await task_tools.task_get(
        TaskGetInput(
            task_id=task_id,
            include=["blocks_outline", "blocks_full", "subtasks", "comments"],
        ),
        _ctx(session),
    )

    assert output.task["blocks_full"] == "## Brief"
    assert output.task["blocks_outline"] == ["Paragraph"]
    assert output.task["subtasks"][0]["subtasks"][0]["title"] == "Draft"
    assert output.task["comments"][0]["content"] == "Comment"


@pytest.mark.asyncio
async def test_task_create_update_and_reschedule_call_service_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, task_id = uuid4(), uuid4()
    created = [{"id": "new-task"}]
    create_tasks = AsyncMock(return_value=created)
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=None))
    monkeypatch.setattr(task_tools, "_create_tasks", create_tasks)
    output = await task_tools.task_create(
        TaskCreateInput(project_id=project_id, tasks=[{"title": "New"}]),
        _ctx(),
    )
    assert output.created == created

    update_api = SimpleNamespace(update_task=AsyncMock(return_value={"status": "done"}))
    update_context = _ctx(service_api=update_api)
    monkeypatch.setattr(task_tools, "_scoped_task", AsyncMock())
    updated = await task_tools.task_update(
        TaskUpdateInput(task_id=task_id, status="done"), update_context
    )
    assert updated.updated_fields == ["status"]
    update_api.update_task.assert_awaited_once()

    reschedule_api = SimpleNamespace(reschedule_task=AsyncMock(return_value={"updated": True}))
    reschedule_context = _ctx(service_api=reschedule_api)
    rescheduled = await task_tools.task_reschedule(
        TaskRescheduleInput(
            task_id=task_id,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 6),
            cascade=True,
        ),
        reschedule_context,
    )
    assert rescheduled.result == {"updated": True}
    reschedule_api.reschedule_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_comment_uses_service_api_for_external_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, project_id = uuid4(), uuid4()
    task = SimpleNamespace(project_id=project_id)
    api = SimpleNamespace(create_comment=AsyncMock(return_value={"comment_id": "c1"}))
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=None))
    monkeypatch.setattr(task_tools, "_scoped_task", AsyncMock(return_value=task))

    result = await task_tools.task_comment(
        task_tools.TaskCommentInput(task_id=task_id, content="Comment"),
        _ctx(service_api=api),
    )

    assert result.id == "c1"
    assert api.create_comment.await_args.args[1]["entity_id"] == str(task_id)


@pytest.mark.asyncio
async def test_task_breakdown_preview_and_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        title="Campaign",
        start_date=date(2026, 11, 5),
        end_date=date(2026, 11, 20),
    )
    context = _ctx(
        _Session(scalar_rows=[[], []]),
        llm=SimpleNamespace(
            chat_json=AsyncMock(
                return_value=(
                    BreakdownPlan(
                        subtasks=[
                            BreakdownItem(
                                client_ref="research",
                                title="Research",
                                duration_days=2,
                                checklist=["Review brief"],
                            )
                        ]
                    ),
                    SimpleNamespace(total_tokens=12),
                )
            )
        ),
    )
    monkeypatch.setattr(task_tools, "_run", AsyncMock(return_value=None))
    monkeypatch.setattr(task_tools, "_scoped_task", AsyncMock(return_value=task))

    preview = await task_tools.task_breakdown(
        task_tools.BreakdownInput(task_id=task.id, mode="preview"),
        context,
    )
    assert preview.subtasks[0]["start_date"] == "2026-11-05"
    assert context.llm_tokens == 12

    apply_context = _ctx(
        _Session(scalar_rows=[[], []]),
        llm=context.services.llm,
    )
    monkeypatch.setattr(task_tools, "_create_tasks", AsyncMock(return_value=[{"id": "c1"}]))
    applied = await task_tools.task_breakdown(
        task_tools.BreakdownInput(task_id=task.id, mode="apply"),
        apply_context,
    )
    assert applied.created == [{"id": "c1"}]

    no_llm = _ctx(_Session(scalar_rows=[[], []]))
    with pytest.raises(ValidationFailed, match="LLM Gateway"):
        await task_tools.task_breakdown(
            task_tools.BreakdownInput(task_id=task.id),
            no_llm,
        )
