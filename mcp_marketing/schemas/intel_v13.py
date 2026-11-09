"""Kontrak 1.3 Dashboard <-> MCP (MCP-V1-00 / DSH-V1-00): job kind v1, event & pagination baru.

Aditif terhadap 1.2 (`schemas/intel.py` tidak diubah): job riset sosial lama tetap memakai
`JobCreateIn` 1.2; job v1 memakai `KindJobCreateIn` dengan `kind` + `params` per kind. Batas &
default HARUS identik dengan Zod Dashboard; keduanya diuji terhadap fixture `contracts/intel-1.3/`
(dibangkitkan `scripts/export_intel_v13_fixtures.py`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from mcp_marketing.schemas.intel import (
    INTEL_EVENT_TYPES,
    IntelWarning,
    McpJobId,
    Period,
    RequestedBy,
    _Strict,
    validate_intel_event_data,
)

CONTRACT_VERSION = "1.3"
CONTRACT_VERSION_PATTERN = r"^1\.([3-9]|[1-9]\d+)$"  # kind v1 butuh >= 1.3

PAGE_LIMIT_MAX = 200
PAGE_LIMIT_DEFAULT = 50
CURSOR_MAX_CHARS = 500
BUDGET_USD_MAX = 100.0
REGIONS_MAX = 50
SEGMENTS_MAX = 8
DISCOVERY_DEFAULT_COMPANIES = 100
DISCOVERY_MAX_COMPANIES = 500
COMPANY_IDS_MAX = 500
PRODUCTS_MAX = 5
SL_KEYWORDS_MAX = 10
CONTENT_DEFAULT_ITEMS = 30
CONTENT_MAX_ITEMS = 100
OBJECTIVE_MAX_CHARS = 1_000
MESSAGE_MAX_CHARS = 300

REGION_CODE_PATTERN = r"^\d{2}(\.\d{2})?$"  # Kemendagri: provinsi "32", kab/kota "32.73"
SHEET_MAX_CHARS = 100

# Toolset baru v1 (daftar tool final ditetapkan di task masing-masing: MCP-V1-05..16).
TOOLSETS_V13: tuple[str, ...] = ("mi", "ci", "kn", "kg", "pb", "opp", "st", "ct")

# Error baru 1.3 (lihat core/errors.py).
ERROR_CODES_V13: tuple[str, ...] = (
    "UPLOAD_TOO_LARGE",
    "UNSUPPORTED_FILE",
    "REVIEW_REQUIRED",
    "CAPTCHA_REQUIRED",
    "LOCAL_MODEL_UNAVAILABLE",
)

JobKind = Literal[
    "company_discovery",
    "company_import",
    "contact_finder",
    "social_listening",
    "book_ingest",
    "opportunity_scoring",
    "strategy",
    "content_bank",
]
JOB_KINDS: tuple[str, ...] = JobKind.__args__  # type: ignore[attr-defined]

# Tahap per kind (urutan eksekusi). `index` & `fit` dijalankan sistem/engine.
KIND_STAGES: dict[str, tuple[str, ...]] = {
    "company_discovery": ("product_analysis", "discovery", "enrichment", "index", "fit"),
    "company_import": ("profile", "mapping", "normalize", "insert"),
    "contact_finder": ("lookup", "verify", "enrichment"),
    "social_listening": ("keywords", "collect", "analyze", "index", "report"),
    "book_ingest": ("extract", "chunk", "embed", "cards", "graph"),
    "opportunity_scoring": ("fit", "score"),
    "strategy": ("insight", "strategy"),
    "content_bank": ("ideate", "evaluate", "originality"),
}
Stage13 = Literal[
    "product_analysis",
    "discovery",
    "enrichment",
    "index",
    "fit",
    "profile",
    "mapping",
    "normalize",
    "insert",
    "lookup",
    "verify",
    "keywords",
    "collect",
    "analyze",
    "report",
    "extract",
    "chunk",
    "embed",
    "cards",
    "graph",
    "score",
    "insight",
    "strategy",
    "ideate",
    "evaluate",
    "originality",
]

JobStatus13 = Literal[
    "queued",
    "running",
    "awaiting_review",
    "paused",
    "completed",
    "failed",
    "cancelling",
    "cancelled",
]
StageStatus13 = Literal[
    "pending", "running", "awaiting_review", "paused", "completed", "skipped", "failed", "cancelled"
]
PauseReason = Literal["captcha", "budget", "manual", "local_model_unavailable"]
ReviewMode = Literal["auto", "review"]
Platform = Literal["instagram", "tiktok", "linkedin", "x", "threads"]
SocialSource = Literal["x", "threads", "trends"]
SOCIAL_SOURCES: tuple[SocialSource, ...] = ("x", "threads", "trends")

RegionCode = Annotated[str, StringConstraints(pattern=REGION_CODE_PATTERN)]
Message = Annotated[str, StringConstraints(max_length=MESSAGE_MAX_CHARS)]


# ---------------------------------------------------------------------------- pagination


class PageIn(_Strict):
    """Pagination seragam semua tool list v1 (cursor opak dari MCP)."""

    cursor: Annotated[str, StringConstraints(max_length=CURSOR_MAX_CHARS)] | None = None
    limit: int = Field(default=PAGE_LIMIT_DEFAULT, ge=1, le=PAGE_LIMIT_MAX)


class PageMeta(BaseModel):
    next_cursor: str | None = None
    total_estimate: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------- params per kind


class CompanyDiscoveryParams(_Strict):
    kind: Literal["company_discovery"]
    product_id: uuid.UUID
    region_codes: list[RegionCode] = Field(default_factory=list, max_length=REGIONS_MAX)
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=SEGMENTS_MAX)
    max_companies: int = Field(
        default=DISCOVERY_DEFAULT_COMPANIES, ge=1, le=DISCOVERY_MAX_COMPANIES
    )
    review_mode: ReviewMode = "review"


class CompanyImportParams(_Strict):
    kind: Literal["company_import"]
    upload_id: uuid.UUID
    sheet: Annotated[str, StringConstraints(max_length=SHEET_MAX_CHARS)] | None = None
    header_row: int | None = Field(default=None, ge=1, le=50)
    mode: Literal["fill_db", "contact_targets"] = "fill_db"


class ContactFinderParams(_Strict):
    kind: Literal["contact_finder"]
    import_batch_id: uuid.UUID | None = None
    company_ids: list[uuid.UUID] = Field(default_factory=list, max_length=COMPANY_IDS_MAX)

    @model_validator(mode="after")
    def _scope(self) -> ContactFinderParams:
        if (self.import_batch_id is None) == (not self.company_ids):
            raise ValueError("isi tepat satu: import_batch_id atau company_ids")
        return self


class SocialListeningParams(_Strict):
    kind: Literal["social_listening"]
    product_id: uuid.UUID
    period: Period
    keywords: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    ] = Field(default_factory=list, max_length=SL_KEYWORDS_MAX)
    sources: list[SocialSource] = Field(
        default_factory=lambda: list(SOCIAL_SOURCES), min_length=1, max_length=3
    )
    review_mode: ReviewMode = "review"

    @model_validator(mode="after")
    def _unique_sources(self) -> SocialListeningParams:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources tidak boleh duplikat")
        return self


class BookIngestParams(_Strict):
    kind: Literal["book_ingest"]
    asset_id: uuid.UUID
    extract_cards: bool = True


class OpportunityScoringParams(_Strict):
    kind: Literal["opportunity_scoring"]
    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=PRODUCTS_MAX)
    company_ids: list[uuid.UUID] = Field(default_factory=list, max_length=COMPANY_IDS_MAX)
    scoring_model_version: int | None = Field(default=None, ge=1)


class StrategyParams(_Strict):
    kind: Literal["strategy"]
    product_id: uuid.UUID
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=SEGMENTS_MAX)
    objective: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=OBJECTIVE_MAX_CHARS)
    ]
    period: Period


class ContentBankParams(_Strict):
    kind: Literal["content_bank"]
    strategy_report_id: uuid.UUID
    item_count: int = Field(default=CONTENT_DEFAULT_ITEMS, ge=1, le=CONTENT_MAX_ITEMS)
    platforms: list[Platform] = Field(min_length=1, max_length=5)
    period: Period | None = None


KindParams = Annotated[
    CompanyDiscoveryParams
    | CompanyImportParams
    | ContactFinderParams
    | SocialListeningParams
    | BookIngestParams
    | OpportunityScoringParams
    | StrategyParams
    | ContentBankParams,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------- input tool


class KindJobEstimateIn(_Strict):
    workspace_id: uuid.UUID
    params: KindParams
    llm_assist: bool = False


class KindJobCreateIn(_Strict):
    """Job v1. `params.kind` menentukan pipeline; `llm_assist` default mati (V1-K12)."""

    contract_version: Annotated[str, StringConstraints(pattern=CONTRACT_VERSION_PATTERN)]
    dashboard_job_id: uuid.UUID
    workspace_id: uuid.UUID
    requested_by: RequestedBy
    params: KindParams
    llm_assist: bool = False
    budget_usd: float | None = Field(default=None, gt=0, le=BUDGET_USD_MAX)
    confirm_cost: bool = False


# ---------------------------------------------------------------------------- output tool

EstimateUnit = Literal[
    "google_search", "google_maps", "web_page", "nominatim", "embedding", "x", "threads", "trends"
]


class EstimateUnitCount(BaseModel):
    unit: EstimateUnit
    count: int = Field(ge=0)


class EstimateLlm13(BaseModel):
    tokens_est: int = Field(ge=0)
    usd_est: float = Field(ge=0)


class KindEstimate(BaseModel):
    seconds: int = Field(ge=0)
    units: list[EstimateUnitCount] = Field(default_factory=list)
    llm: EstimateLlm13
    requires_confirmation: bool
    warnings: list[IntelWarning] = Field(default_factory=list)


class KindJobAcceptedOut(BaseModel):
    accepted: Literal[True] = True
    mcp_job_id: str = Field(pattern=r"^j_[0-9a-z]{10}$")
    kind: JobKind
    estimate: KindEstimate


class Progress13(BaseModel):
    done: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)


class ErrorInfo13(BaseModel):
    code: str = Field(max_length=48)
    message: str = Field(max_length=500)
    retryable: bool
    stage: Stage13 | None = None


class StageView13(BaseModel):
    stage: Stage13
    agent_key: str | None = None  # None = tahap sistem (index, fit, normalize)
    status: StageStatus13
    progress: Progress13 = Field(default_factory=lambda: Progress13(done=0))
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorInfo13 | None = None


class PauseInfo(BaseModel):
    reason: PauseReason
    stage: Stage13
    message: str = Field(max_length=MESSAGE_MAX_CHARS)
    since: AwareDatetime


class ReviewInfo(BaseModel):
    stage: Stage13
    subject_type: str = Field(max_length=48)
    subject_id: str = Field(max_length=64)


class StageCost13(BaseModel):
    stage: Stage13
    agent_key: str | None = None
    llm_tokens: int = Field(ge=0)
    usd_llm: float = Field(ge=0)


class CostView13(BaseModel):
    llm_tokens: int = Field(default=0, ge=0)
    usd_llm: float = Field(default=0, ge=0)
    external_units: int = Field(default=0, ge=0)
    usd_external: float = Field(default=0, ge=0)
    by_stage: list[StageCost13] = Field(default_factory=list)


class KindJobView(BaseModel):
    """`result` per kind ditetapkan task pipeline masing-masing (MCP-V1-08..16)."""

    mcp_job_id: str = Field(pattern=r"^j_[0-9a-z]{10}$")
    dashboard_job_id: uuid.UUID
    kind: JobKind
    status: JobStatus13
    llm_assist: bool
    budget_usd: float | None = None
    stages: list[StageView13]
    paused: PauseInfo | None = None
    review: ReviewInfo | None = None
    result: dict[str, Any] | None = None
    cost: CostView13
    error: ErrorInfo13 | None = None

    @model_validator(mode="after")
    def _status_details(self) -> KindJobView:
        if self.status == "paused" and self.paused is None:
            raise ValueError("status paused butuh paused")
        if self.status == "awaiting_review" and self.review is None:
            raise ValueError("status awaiting_review butuh review")
        return self


# ---------------------------------------------------------------------------- event webhook

EVENT_TYPES_V13: tuple[str, ...] = (
    "intel.job.awaiting_review",
    "intel.job.paused",
    "intel.company.upserted",
    "ci.import.status",
    "kn.asset.status",
    "opp.scores.updated",
    "ct.batch.ready",
)
ALL_EVENT_TYPES: tuple[str, ...] = INTEL_EVENT_TYPES + EVENT_TYPES_V13
JOB_SCOPED_PREFIX = "intel.job."


class AwaitingReviewData(_Strict):
    stage: Stage13
    subject_type: Literal[
        "product_analysis", "keyword_set", "strategy", "content_batch", "import_review"
    ]
    subject_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    message: Message | None = None


class PausedData(_Strict):
    stage: Stage13
    reason: PauseReason
    message: Message
    resume_hint: Message | None = None


class CompanyUpsertedData(_Strict):
    company_id: uuid.UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    action: Literal["created", "updated", "merged"]
    origin: Literal["discovery", "import", "manual"]
    merged_into: uuid.UUID | None = None


class ImportStatusData(_Strict):
    batch_id: uuid.UUID
    status: Literal["profiling", "mapping", "importing", "completed", "failed", "rolled_back"]
    rows_total: int = Field(ge=0)
    rows_ok: int = Field(ge=0)
    rows_review: int = Field(ge=0)
    rows_failed: int = Field(ge=0)

    @model_validator(mode="after")
    def _counts(self) -> ImportStatusData:
        if self.rows_ok + self.rows_review + self.rows_failed > self.rows_total:
            raise ValueError("rows_ok + rows_review + rows_failed melebihi rows_total")
        return self


class AssetStatusData(_Strict):
    asset_id: uuid.UUID
    status: Literal["registered", "extracting", "embedding", "carding", "ready", "failed"]
    pages_done: int = Field(default=0, ge=0)
    pages_total: int | None = Field(default=None, ge=0)
    message: Message | None = None


class ScoresUpdatedData(_Strict):
    product_id: uuid.UUID
    scoring_model_version: int = Field(ge=1)
    scored: int = Field(ge=0)
    high: int = Field(ge=0)
    medium: int = Field(ge=0)


class ContentBatchReadyData(_Strict):
    batch_id: uuid.UUID
    strategy_report_id: uuid.UUID
    items: int = Field(ge=0)
    flagged: int = Field(default=0, ge=0)


EVENT_DATA_MODELS_V13: dict[str, type[BaseModel]] = {
    "intel.job.awaiting_review": AwaitingReviewData,
    "intel.job.paused": PausedData,
    "intel.company.upserted": CompanyUpsertedData,
    "ci.import.status": ImportStatusData,
    "kn.asset.status": AssetStatusData,
    "opp.scores.updated": ScoresUpdatedData,
    "ct.batch.ready": ContentBatchReadyData,
}

EventType13 = Literal[
    "intel.job.started",
    "intel.job.stage.started",
    "intel.job.progress",
    "intel.job.stage.completed",
    "intel.job.completed",
    "intel.job.failed",
    "intel.job.cancelled",
    "intel.job.awaiting_review",
    "intel.job.paused",
    "intel.company.upserted",
    "ci.import.status",
    "kn.asset.status",
    "opp.scores.updated",
    "ct.batch.ready",
]


class EventEnvelope13(_Strict):
    """`job_id`/`mcp_job_id` wajib untuk `intel.job.*`; event entitas boleh tanpa job."""

    contract_version: Annotated[str, StringConstraints(pattern=r"^1\.\d+$")]
    event_id: uuid.UUID
    event_type: EventType13
    occurred_at: AwareDatetime
    workspace_id: uuid.UUID
    job_id: uuid.UUID | None = None
    mcp_job_id: McpJobId | None = None
    sequence: int = Field(ge=1)
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _job_scope(self) -> EventEnvelope13:
        if self.event_type.startswith(JOB_SCOPED_PREFIX) and (
            self.job_id is None or self.mcp_job_id is None
        ):
            raise ValueError("event intel.job.* butuh job_id dan mcp_job_id")
        return self


def validate_event_data_v13(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validasi `data` event 1.3 (termasuk 7 event 1.2). ValueError bila tidak dikenal/invalid."""
    model = EVENT_DATA_MODELS_V13.get(event_type)
    if model is None:
        return validate_intel_event_data(event_type, data)
    return model.model_validate(data).model_dump(mode="json", exclude_none=True)
