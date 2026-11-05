"""Kontrak 1.2 Dashboard <-> MCP untuk toolset `intel` (Modul 3 Dashboard, M3_MCP_INTEGRATION.md).

Sumber kebenaran schema di sisi MCP. Batas & default HARUS identik dengan Zod Dashboard
(`backend/src/shared/schemas/intel.schema.ts`); keduanya diuji terhadap fixture bersama
`contracts/intel-1.2/` (dibangkitkan `scripts/export_intel_fixtures.py`).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "1.2"

MAX_SOURCES = 4
KEYWORDS_MAX = 10
KEYWORD_MAX_CHARS = 100
TRENDS_KEYWORDS_MAX = 5
X_MAX_ITEMS = 5_000
X_DEFAULT_ITEMS = 500
THREADS_MAX_ITEMS = 2_000
THREADS_DEFAULT_ITEMS = 300
URLS_MAX = 50
URL_MAX_CHARS = 2_048
CRAWL_MAX_PAGES = 200
CRAWL_DEFAULT_PAGES = 50
CRAWL_MAX_DEPTH = 3
ASPECTS_MAX = 10
ASPECT_MAX_CHARS = 40
PERIOD_MAX_DAYS = 366
TITLE_MAX_CHARS = 150
ITEMS_PAGE_MAX = 100
ITEMS_PAGE_DEFAULT = 50
QUERY_MAX_CHARS = 100
SUMMARY_MD_MAX_CHARS = 4_000
SUMMARY_PREVIEW_MAX_CHARS = 500
RECOMMENDATIONS_MAX = 5
TOP_MAX = 10
EXAMPLES_MAX = 3

AGENT_KEY_PATTERN = r"^[a-z][a-z0-9_]{1,99}$"
MCP_JOB_ID_PATTERN = r"^j_[0-9a-z]{10}$"
HANDLE_PATTERN = r"^h_[0-9a-z]{10}$"

Stage = Literal["collect", "analyze", "index"]
SourceType = Literal["x", "threads", "web", "trends"]
JobStatus = Literal[
    "queued",
    "collecting",
    "analyzing",
    "indexing",
    "completed",
    "failed",
    "cancelling",
    "cancelled",
]
StageStatus = Literal["pending", "running", "completed", "skipped", "failed", "cancelled"]
Sentiment = Literal["positive", "neutral", "negative"]
Capability = Literal["collect", "analyze", "index"]
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

Keyword = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=KEYWORD_MAX_CHARS)
]
Url = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=URL_MAX_CHARS, pattern=r"^https?://[^\s/$.?#][^\s]*$"
    ),
]
AgentKey = Annotated[str, StringConstraints(pattern=AGENT_KEY_PATTERN)]
McpJobId = Annotated[str, StringConstraints(pattern=MCP_JOB_ID_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------- JobSpec


class XSource(_Strict):
    type: Literal["x"]
    keywords: list[Keyword] = Field(min_length=1, max_length=KEYWORDS_MAX)
    match: Literal["any", "all"] = "any"
    lang: str | None = Field(default="id", max_length=8)
    exclude_retweets: bool = True
    exclude_replies: bool = False
    max_items: int = Field(default=X_DEFAULT_ITEMS, ge=1, le=X_MAX_ITEMS)
    provider: Literal["auto", "x_api", "browser"] = "auto"


class ThreadsSource(_Strict):
    type: Literal["threads"]
    keywords: list[Keyword] = Field(min_length=1, max_length=KEYWORDS_MAX)
    search_type: Literal["TOP", "RECENT"] = "RECENT"
    max_items: int = Field(default=THREADS_DEFAULT_ITEMS, ge=1, le=THREADS_MAX_ITEMS)


class WebSource(_Strict):
    type: Literal["web"]
    mode: Literal["scrape", "crawl"]
    urls: list[Url] | None = Field(default=None, min_length=1, max_length=URLS_MAX)
    start_url: Url | None = None
    max_pages: int = Field(default=CRAWL_DEFAULT_PAGES, ge=1, le=CRAWL_MAX_PAGES)
    max_depth: int = Field(default=2, ge=0, le=CRAWL_MAX_DEPTH)
    include_patterns: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=10
    )

    @model_validator(mode="after")
    def _mode_fields(self) -> WebSource:
        if self.mode == "scrape" and not self.urls:
            raise ValueError("mode scrape butuh urls")
        if self.mode == "crawl" and not self.start_url:
            raise ValueError("mode crawl butuh start_url")
        return self


class TrendsSource(_Strict):
    type: Literal["trends"]
    keywords: list[Keyword] = Field(min_length=1, max_length=TRENDS_KEYWORDS_MAX)
    geo: str = Field(default="ID", pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")


Source = Annotated[XSource | ThreadsSource | WebSource | TrendsSource, Field(discriminator="type")]


class Period(_Strict):
    since: date
    until: date

    @model_validator(mode="after")
    def _range(self) -> Period:
        if self.until < self.since:
            raise ValueError("until tidak boleh sebelum since")
        if (self.until - self.since).days + 1 > PERIOD_MAX_DAYS:
            raise ValueError(f"rentang periode maksimal {PERIOD_MAX_DAYS} hari")
        return self


class StageAgent(_Strict):
    agent_key: AgentKey


class AnalyzeStage(StageAgent):
    aspects: list[
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ASPECT_MAX_CHARS)
        ]
    ] = Field(default_factory=list, max_length=ASPECTS_MAX)
    method: Literal["auto", "model", "llm"] = "auto"


class IndexStage(StageAgent):
    enabled: bool = False


class Stages(_Strict):
    collect: StageAgent
    analyze: AnalyzeStage
    index: IndexStage | None = None


class JobSpec(_Strict):
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_CHARS)
    ]
    language: Literal["id", "en"] = "id"
    period: Period
    sources: list[Source] = Field(min_length=1, max_length=MAX_SOURCES)
    stages: Stages

    @field_validator("sources")
    @classmethod
    def _unique_types(cls, v: list[Any]) -> list[Any]:
        types = [s.type for s in v]
        if len(types) != len(set(types)):
            raise ValueError("maksimal satu sumber per type")
        return v

    def source_types(self) -> list[str]:
        return [s.type for s in self.sources]


# ---------------------------------------------------------------------------- input tool


class RequestedBy(_Strict):
    user_id: uuid.UUID
    name: Annotated[str, StringConstraints(max_length=100)]


class AgentsListIn(_Strict):
    pass


class SourcesStatusIn(_Strict):
    refresh: bool = False


class JobEstimateIn(_Strict):
    workspace_id: uuid.UUID
    spec: JobSpec


class JobCreateIn(_Strict):
    contract_version: Annotated[str, StringConstraints(pattern=r"^1\.\d+$")]
    dashboard_job_id: uuid.UUID
    workspace_id: uuid.UUID
    requested_by: RequestedBy
    spec: JobSpec
    confirm_cost: bool = False


class JobRefIn(_Strict):
    workspace_id: uuid.UUID
    mcp_job_id: McpJobId


class ItemsFilter(_Strict):
    source: SourceType | None = None
    sentiment: Sentiment | None = None
    q: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=QUERY_MAX_CHARS)] | None
    ) = None


class ItemsIn(JobRefIn):
    filter: ItemsFilter = Field(default_factory=ItemsFilter)
    order_by: Literal["-created_at", "created_at", "-engagement"] = "-created_at"
    limit: int = Field(default=ITEMS_PAGE_DEFAULT, ge=1, le=ITEMS_PAGE_MAX)
    cursor: Annotated[str, StringConstraints(max_length=500)] | None = None


class LlmStatusIn(_Strict):
    range_days: Literal[7, 30] = 7


# ---------------------------------------------------------------------------- output tool


class GatewayKeyInfo(BaseModel):
    source: Literal["agent", "system", "env"]
    prefix: str | None = None


class AgentInfo(BaseModel):
    agent_key: str
    display_name: str
    is_active: bool
    route: str
    run_token_budget: int
    toolsets: list[str]
    capabilities: list[Capability]
    gateway_key: GatewayKeyInfo


class AgentsListOut(BaseModel):
    agents: list[AgentInfo]


class SourceStatus(BaseModel):
    source: SourceType
    available: bool
    mode: Literal["api", "browser", "unofficial", "none"]
    detail: str = Field(max_length=300)
    checked_at: AwareDatetime


class RouteStatus(BaseModel):
    route: str
    available: bool


class LlmRoutes(BaseModel):
    routes: list[RouteStatus]


class SourcesStatusOut(BaseModel):
    sources: list[SourceStatus]
    llm: LlmRoutes
    checked_at: AwareDatetime


class EstimateSource(BaseModel):
    type: SourceType
    mode: Literal["api", "browser", "unofficial", "none"]
    external_units: int = Field(ge=0)
    usd_external: float = Field(ge=0)


class EstimateStage(BaseModel):
    stage: Stage
    agent_key: str
    route: str
    tokens_est: int = Field(ge=0)


class EstimateLlm(BaseModel):
    tokens_est: int = Field(ge=0)
    usd_est: float = Field(ge=0)
    by_stage: list[EstimateStage]


class IntelWarning(BaseModel):
    code: str = Field(max_length=48)
    message: str = Field(max_length=300)


class Estimate(BaseModel):
    seconds: int = Field(ge=0)
    sources: list[EstimateSource]
    llm: EstimateLlm
    requires_confirmation: bool
    warnings: list[IntelWarning] = Field(default_factory=list)


class JobAcceptedOut(BaseModel):
    accepted: Literal[True] = True
    mcp_job_id: str = Field(pattern=MCP_JOB_ID_PATTERN)
    estimate: Estimate


class Progress(BaseModel):
    done: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)


class ErrorInfo(BaseModel):
    code: str = Field(max_length=48)
    message: str = Field(max_length=500)
    retryable: bool


class StageErrorInfo(ErrorInfo):
    stage: Stage | None = None


class StageView(BaseModel):
    stage: Stage
    agent_key: str
    status: StageStatus
    progress: Progress = Field(default_factory=lambda: Progress(done=0))
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorInfo | None = None


class TableData(BaseModel):
    cols: list[str]
    rows: list[list[Any]]


class SentimentDistribution(BaseModel):
    positive: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    negative: float = Field(ge=0, le=1)


class Example(BaseModel):
    id: str
    source: str
    author: str | None = None
    text: str = Field(max_length=280)
    url: str | None = None
    created_at: str | None = None


class Examples(BaseModel):
    positive: list[Example] = Field(default_factory=list, max_length=EXAMPLES_MAX)
    negative: list[Example] = Field(default_factory=list, max_length=EXAMPLES_MAX)


class SentimentResult(BaseModel):
    distribution: SentimentDistribution
    net_sentiment: float = Field(ge=-1, le=1)
    by_day: TableData
    aspects: TableData | None = None
    examples: Examples


class Topic(BaseModel):
    label: str = Field(max_length=80)
    count: int = Field(ge=0)
    share: float = Field(ge=0, le=1)


class TrendsResult(BaseModel):
    topics: list[Topic] = Field(default_factory=list, max_length=TOP_MAX)
    hashtags: list[tuple[str, int]] = Field(default_factory=list, max_length=TOP_MAX)
    authors: list[tuple[str, int]] = Field(default_factory=list, max_length=TOP_MAX)


class Totals(BaseModel):
    items: int = Field(ge=0)
    by_source: dict[str, int]


class JobResultView(BaseModel):
    totals: Totals
    sentiment: SentimentResult | None = None
    trends: TrendsResult
    volume_by_day: TableData
    google_trends: dict[str, Any] | None = None
    summary_md: str | None = Field(default=None, max_length=SUMMARY_MD_MAX_CHARS)
    recommendations: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list, max_length=RECOMMENDATIONS_MAX
    )
    items_handle: str | None = Field(default=None, pattern=HANDLE_PATTERN)
    items_expire_at: AwareDatetime | None = None
    partial: bool = False
    warnings: list[IntelWarning] = Field(default_factory=list)


class StageCost(BaseModel):
    stage: Stage
    agent_key: str
    llm_tokens: int = Field(ge=0)
    usd_llm: float = Field(ge=0)


class CostView(BaseModel):
    llm_tokens: int = Field(default=0, ge=0)
    usd_llm: float = Field(default=0, ge=0)
    external_units: int = Field(default=0, ge=0)
    usd_external: float = Field(default=0, ge=0)
    by_stage: list[StageCost] = Field(default_factory=list)


class JobView(BaseModel):
    mcp_job_id: str = Field(pattern=MCP_JOB_ID_PATTERN)
    dashboard_job_id: uuid.UUID
    status: JobStatus
    stages: list[StageView]
    result: JobResultView | None = None
    cost: CostView
    error: StageErrorInfo | None = None


class CancelOut(BaseModel):
    cancelled: bool
    status: JobStatus


class PurgeIndexOut(BaseModel):
    deleted_documents: int = Field(ge=0)
    deleted_chunks: int = Field(ge=0)


class ItemsOut(BaseModel):
    cols: list[str]
    rows: list[list[Any]]
    total: int = Field(ge=0)
    next_cursor: str | None = None
    expires_at: AwareDatetime | None = None


class AgentUsage(BaseModel):
    calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    usd: float = Field(ge=0)


class AgentLlmStatus(BaseModel):
    agent_key: str
    key_source: Literal["agent", "system", "env"]
    key_prefix: str | None = None
    key_status: Literal["active", "revoked", "invalid", "unknown"]
    last_error: str | None = None
    usage: AgentUsage


class ProjectUsage(BaseModel):
    slug: str | None = None
    start: date
    end: date
    data: list[dict[str, Any]]


class LlmStatusOut(BaseModel):
    agents: list[AgentLlmStatus]
    project: ProjectUsage | None = None


# ---------------------------------------------------------------------------- event webhook

INTEL_EVENT_TYPES = (
    "intel.job.started",
    "intel.job.stage.started",
    "intel.job.progress",
    "intel.job.stage.completed",
    "intel.job.completed",
    "intel.job.failed",
    "intel.job.cancelled",
)
IntelEventType = Literal[
    "intel.job.started",
    "intel.job.stage.started",
    "intel.job.progress",
    "intel.job.stage.completed",
    "intel.job.completed",
    "intel.job.failed",
    "intel.job.cancelled",
]


class EmptyData(_Strict):
    pass


class StageStartedData(_Strict):
    stage: Stage
    agent_key: AgentKey


class ProgressData(_Strict):
    stage: Stage
    done: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    message: Annotated[str, StringConstraints(max_length=300)] | None = None


class StageCompletedData(_Strict):
    stage: Stage
    status: Literal["completed", "skipped"] = "completed"
    stats: dict[str, Any] | None = None
    warnings: list[IntelWarning] = Field(default_factory=list)


class CompletedData(_Strict):
    summary_preview: Annotated[str, StringConstraints(max_length=SUMMARY_PREVIEW_MAX_CHARS)] = ""
    totals: Totals
    cost: CostView
    partial: bool = False


class FailedData(_Strict):
    stage: Stage | None = None
    error: ErrorInfo


EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "intel.job.started": EmptyData,
    "intel.job.stage.started": StageStartedData,
    "intel.job.progress": ProgressData,
    "intel.job.stage.completed": StageCompletedData,
    "intel.job.completed": CompletedData,
    "intel.job.failed": FailedData,
    "intel.job.cancelled": EmptyData,
}


class IntelEventEnvelope(_Strict):
    contract_version: Annotated[str, StringConstraints(pattern=r"^1\.\d+$")]
    event_id: uuid.UUID
    event_type: IntelEventType
    occurred_at: AwareDatetime
    job_id: uuid.UUID
    mcp_job_id: McpJobId
    sequence: int = Field(ge=1)
    data: dict[str, Any] = Field(default_factory=dict)


def validate_intel_event_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validasi `data` event; kembalikan bentuk JSON bersih. ValueError bila tidak valid."""
    model = EVENT_DATA_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"event_type intel tidak dikenal: {event_type}")
    return model.model_validate(data).model_dump(mode="json", exclude_none=True)
