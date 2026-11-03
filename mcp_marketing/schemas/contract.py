"""Kontrak Dashboard 1.0 (M1_MCP_INTEGRATION §3, §5; `mcp.schema.ts`) dalam Pydantic.

Input dari Dashboard dibuat toleran (`extra="ignore"`: penambahan field di contract 1.x minor tidak
memutus). Payload KELUAR (webhook) dibuat ketat agar setara Zod Dashboard.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from mcp_marketing.schemas.blocks import validate_block_content

CONTRACT_MAJOR = 1
CONTRACT_RE = re.compile(r"^1\.\d+$")

MAX_BLOCK_OPS = 50
MAX_SUBTASKS = 20
MAX_DEPTH = 3
MAX_BODY = 1_000_000
MAX_COMMENT = 10_000
MAX_BLOCK_JSON = 65_536
MAX_CONTEXT_BLOCKS = 200

EVENT_TYPES = (
    "agent.task.started",
    "agent.status.changed",
    "agent.block.appended",
    "agent.subtask.created",
    "agent.comment.added",
    "agent.task.completed",
    "agent.task.failed",
)
EventType = Literal[
    "agent.task.started",
    "agent.status.changed",
    "agent.block.appended",
    "agent.subtask.created",
    "agent.comment.added",
    "agent.task.completed",
    "agent.task.failed",
]
TaskPriority = Literal["none", "low", "medium", "high", "urgent"]


def _lower_uuid(v: str) -> str:
    return str(uuid.UUID(v)).lower()


def _date_str(v: str) -> str:
    date.fromisoformat(v)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError("Format tanggal harus YYYY-MM-DD")
    return v


Uuid = Annotated[str, AfterValidator(_lower_uuid)]
DateStr = Annotated[str, AfterValidator(_date_str)]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ input dari Dashboard
class TaskSnapshot(_In):
    id: Uuid
    project_id: Uuid
    workspace_id: Uuid | None = None
    parent_task_id: Uuid | None = None
    depth: int = 1
    title: str
    status: str = "todo"
    priority: str = "none"
    start_date: str | None = None
    end_date: str | None = None
    is_milestone: bool = False
    tags: list[str] = Field(default_factory=list)
    url: str | None = None


class ContextBlock(_In):
    id: str
    parent_block_id: str | None = None
    block_type: str
    content_json: Any = None
    plain_text: str = ""
    version: int = 1


class SubtaskRef(_In):
    id: str
    title: str
    status: str | None = None
    depth: int | None = None


class Person(_In):
    user_id: str | None = None
    display_name: str | None = None


class Author(_In):
    type: str = "user"
    id: str | None = None
    display_name: str | None = None


class Callback(_In):
    webhook_url: str
    agent_api_base_url: str


class Limits(_In):
    max_depth: int = MAX_DEPTH
    max_subtasks_per_event: int = MAX_SUBTASKS
    max_blocks_per_event: int = MAX_BLOCK_OPS


class ContractInput(_In):
    contract_version: str = Field(description="Versi kontrak Dashboard, mis. 1.0")
    run_id: Uuid


class DispatchInput(ContractInput):
    agent_key: str = Field(max_length=100)
    agent_id: Uuid
    task: TaskSnapshot
    task_description: str = ""
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    subtasks: list[SubtaskRef] = Field(default_factory=list)
    dispatched_by: Person | None = None
    callback: Callback
    limits: Limits | None = None


class EstimationInput(ContractInput):
    agent_key: str = Field(max_length=100)
    task: TaskSnapshot
    task_description: str = ""
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    earliest_start_date: DateStr
    timezone: str = "Asia/Jakarta"


class CommentIn(_In):
    id: str
    parent_comment_id: str | None = None
    content: str
    author: Author = Field(default_factory=Author)
    created_at: str | None = None


class ThreadItem(_In):
    id: str
    content: str
    author: Author = Field(default_factory=Author)


class MentionInput(ContractInput):
    agent_key: str = Field(max_length=100)
    agent_id: Uuid
    task: TaskSnapshot | None = None
    project_id: Uuid
    comment: CommentIn
    thread: list[ThreadItem] = Field(default_factory=list)
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    callback: Callback


class PromptBlockRef(_In):
    id: Uuid
    prompt: str = ""
    version: int = 1


class PromptBlockInput(ContractInput):
    agent_key: str = Field(max_length=100)
    agent_id: Uuid
    task: TaskSnapshot | None = None
    project_id: Uuid
    prompt_block: PromptBlockRef
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    callback: Callback


class CancelInput(ContractInput):
    mcp_run_id: str | None = None
    reason: str = "user_cancelled"


# ------------------------------------------------------------------ output ke Dashboard
class DispatchOutput(BaseModel):
    accepted: bool
    mcp_run_id: str | None = Field(default=None, max_length=200)


class EstimationOutput(BaseModel):
    start_date: DateStr
    end_date: DateStr
    estimated_days: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    message: str | None = Field(default=None, max_length=2000)


class CancelOutput(BaseModel):
    cancelled: bool


# ------------------------------------------------------------------ webhook (ketat)
class BlockInput(_Out):
    id: Uuid | None = None
    parent_block_id: Uuid | None = None
    block_type: Annotated[str, StringConstraints(max_length=32)]
    content_json: Any = None

    @model_validator(mode="after")
    def _content(self) -> BlockInput:
        self.content_json = validate_block_content(self.block_type, self.content_json)
        return self


class BlockUpdateInput(_Out):
    id: Uuid
    parent_block_id: Uuid | None = None
    block_type: Annotated[str, StringConstraints(max_length=32)] | None = None
    content_json: Any = None

    @model_validator(mode="after")
    def _content(self) -> BlockUpdateInput:
        if self.block_type is not None:
            self.content_json = validate_block_content(self.block_type, self.content_json)
        return self


class AppendOp(_Out):
    op: Literal["append"]
    block: BlockInput
    after_block_id: Uuid | None = None


class UpdateOp(_Out):
    op: Literal["update"]
    block: BlockUpdateInput
    expected_version: int | None = Field(default=None, gt=0)


BlockOp = Annotated[AppendOp | UpdateOp, Field(discriminator="op")]


class StartedData(_Out):
    pass


class StatusChangedData(_Out):
    status: Literal["thinking", "working"]
    message: str | None = Field(default=None, max_length=500)


class BlockAppendedData(_Out):
    operations: list[BlockOp] = Field(min_length=1, max_length=MAX_BLOCK_OPS)


Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class InitialBlock(_Out):
    block_type: Annotated[str, StringConstraints(max_length=32)]
    content_json: Any = None

    @model_validator(mode="after")
    def _content(self) -> InitialBlock:
        self.content_json = validate_block_content(self.block_type, self.content_json)
        return self


class SubtaskSpec(_Out):
    client_ref: str | None = Field(default=None, max_length=100)
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    priority: TaskPriority | None = None
    start_date: DateStr | None = None
    end_date: DateStr | None = None
    tags: list[Tag] | None = Field(default=None, max_length=20)
    assign_to_self: bool = False
    initial_blocks: list[InitialBlock] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _dates(self) -> SubtaskSpec:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date harus >= start_date")
        return self


class SubtaskCreatedData(_Out):
    parent_task_id: Uuid | None = None
    subtasks: list[SubtaskSpec] = Field(min_length=1, max_length=MAX_SUBTASKS)


class MentionRef(_Out):
    type: Literal["user", "agent"]
    id: Uuid


class CommentAddedData(_Out):
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMMENT)
    ]
    parent_comment_id: Uuid | None = None
    mentions: list[MentionRef] | None = Field(default=None, max_length=50)


class TaskCompletedData(_Out):
    summary: str | None = Field(default=None, max_length=10_000)


class TaskFailedData(_Out):
    error_code: str = Field(max_length=100)
    message: str = Field(max_length=2000)
    retryable: bool | None = None


EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "agent.task.started": StartedData,
    "agent.status.changed": StatusChangedData,
    "agent.block.appended": BlockAppendedData,
    "agent.subtask.created": SubtaskCreatedData,
    "agent.comment.added": CommentAddedData,
    "agent.task.completed": TaskCompletedData,
    "agent.task.failed": TaskFailedData,
}


class WebhookEnvelope(_Out):
    contract_version: str
    event_id: Uuid
    event_type: EventType
    occurred_at: str
    run_id: Uuid
    agent_id: Uuid
    task_id: Uuid | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("contract_version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not CONTRACT_RE.match(v):
            raise ValueError("contract_version tidak didukung (harus 1.x)")
        return v


def validate_event_data(event_type: str, data: dict[str, Any] | BaseModel) -> dict[str, Any]:
    """Validasi data event dengan schema kontrak; kembalikan dict bersih (tanpa None)."""
    model = EVENT_DATA_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"event_type tidak dikenal: {event_type}")
    raw = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
    return model.model_validate(raw).model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]


def is_supported_contract(version: str) -> bool:
    return bool(CONTRACT_RE.match(version or ""))
