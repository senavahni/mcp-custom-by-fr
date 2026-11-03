"""Schema I/O bersama semua tool (MCP_SPEC §2): parameter umum, envelope, job, error."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResponseFormat = Literal["concise", "detailed"]
CacheMode = Literal["use", "bypass"]


class ToolInput(BaseModel):
    """Input tool: field tak dikenal ditolak agar LLM cepat tahu salah nama parameter."""

    model_config = ConfigDict(extra="forbid")


class ReadInput(ToolInput):
    response_format: ResponseFormat = Field(
        default="concise",
        description="concise = ringkas (maks 5 contoh); detailed = hingga 20 item",
    )
    cache: CacheMode = Field(default="use", description="bypass untuk memaksa hitung ulang")


class Table(BaseModel):
    """Format kolom-baris (hemat token dibanding list objek)."""

    cols: list[str]
    rows: list[list[Any]]


class Cost(BaseModel):
    llm_tokens: int = 0
    external_units: int = 0
    cache_hit: bool = False


class Envelope(BaseModel):
    """Envelope output standar tool data (MCP_SPEC §2.2)."""

    summary: str = Field(max_length=300)
    handle: str | None = None
    total: int = 0
    stats: dict[str, Any] = Field(default_factory=dict)
    items: Table | None = None
    next_cursor: str | None = None
    truncated: bool = False
    hint: str | None = None
    cost: Cost = Field(default_factory=Cost)


class JobEstimate(BaseModel):
    seconds: int = 0
    external_units: int = 0
    usd: float = 0.0


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"
    estimate: JobEstimate | None = None
    poll_after_s: int = 10


class ToolErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None


def to_table(rows: list[dict[str, Any]], cols: list[str]) -> Table:
    return Table(cols=cols, rows=[[row.get(c) for c in cols] for row in rows])
