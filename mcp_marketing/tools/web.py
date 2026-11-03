"""Toolset `web`: profil browser & scraping Selenium (MCP_SPEC §4.5, TASK-14)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mcp_marketing.core.errors import Forbidden, ValidationFailed
from mcp_marketing.schemas.common import Envelope, ReadInput, Table, ToolInput
from mcp_marketing.server.registry import ToolContext, tool
from mcp_marketing.services.browser.driver_pool import profile_store
from mcp_marketing.services.browser.profiles import profile_view
from mcp_marketing.services.browser.ssrf import parse_http_url
from mcp_marketing.tools._jobs import enqueue_and_wait
from mcp_marketing.worker.handlers.web import normalize_url

HANDLE_PATTERN = r"^h_[0-9a-z]{10}$"
PROFILE_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,62}$"


class ProfileListIn(ToolInput):
    pass


class ProfileListOut(BaseModel):
    summary: str
    items: Table


@tool(
    name="browser_profile_list",
    toolset="web",
    summary="Daftar profil Chrome terdaftar + status login",
    description=(
        "List registered Chrome profiles (purpose, logged-in sites, lock state, last use)."
    ),
    input_model=ProfileListIn,
    output_model=ProfileListOut,
)
async def browser_profile_list(inp: ProfileListIn, ctx: ToolContext) -> ProfileListOut:
    profiles = [profile_view(p) for p in await profile_store(ctx.services).list()]
    cols = ["name", "purpose", "logged_in_sites", "locked", "last_used"]
    return ProfileListOut(
        summary=f"{len(profiles)} profil aktif",
        items=Table(cols=cols, rows=[[p[c] for c in cols] for p in profiles]),
    )


class ProfileCheckIn(ToolInput):
    profile: str = Field(pattern=PROFILE_PATTERN)
    sites: (
        list[Literal["x", "threads", "google", "x.com", "threads.net", "trends.google.com"]] | None
    ) = None
    wait_s: int = Field(default=15, ge=0, le=20)


class SiteCheck(BaseModel):
    site: str
    logged_in: bool | None
    detail: str


class ProfileCheckOut(BaseModel):
    summary: str
    profile: str
    checks: list[SiteCheck]


@tool(
    name="browser_profile_check",
    toolset="web",
    summary="Cek status login profil per situs (job singkat, admin)",
    description=(
        "Open the profile in Chrome and check login state per site (x, threads, google). Admin "
        "only."
    ),
    input_model=ProfileCheckIn,
    output_model=ProfileCheckOut,
    returns_job=True,
    admin_only=True,
    writes=True,
)
async def browser_profile_check(inp: ProfileCheckIn, ctx: ToolContext) -> Any:
    await profile_store(ctx.services).get(inp.profile)
    return await enqueue_and_wait(
        ctx,
        "browser_profile_check",
        {"profile": inp.profile, "sites": inp.sites},
        wait_s=inp.wait_s,
        timeout_s=300,
    )


class ScrapeIn(ReadInput):
    urls: list[str] = Field(min_length=1, max_length=50)
    profile: str = Field(default="default", pattern=PROFILE_PATTERN)
    mode: Literal["text", "markdown", "selectors", "links"] = "markdown"
    selectors: dict[str, str] | None = Field(
        default=None, description="{nama: css} atau css::attr(href)"
    )
    wait_for: str | None = Field(
        default=None, max_length=200, description="CSS yang ditunggu muncul"
    )
    scroll: int = Field(default=0, ge=0, le=20)
    max_chars_per_page: int = Field(default=20_000, ge=200, le=20_000)
    respect_robots: bool = True
    wait_s: int = Field(default=15, ge=0, le=20)

    @model_validator(mode="after")
    def _check(self) -> ScrapeIn:
        for url in self.urls:
            parse_http_url(url)
        if self.mode == "selectors" and not self.selectors:
            raise ValueError("mode selectors butuh selectors")
        if self.selectors and len(self.selectors) > 50:
            raise ValueError("selectors maksimal 50")
        return self


class PageOut(BaseModel):
    url: str
    title: str | None = None
    status: str
    chars: int = 0
    content: str = ""


class ScrapeOut(Envelope):
    pages: list[PageOut] | None = None


def _respect_robots(inp_value: bool, ctx: ToolContext) -> bool:
    if not inp_value and not ctx.client.is_admin:
        raise Forbidden("respect_robots=false hanya untuk klien admin")
    return inp_value


@tool(
    name="web_scrape",
    toolset="web",
    summary="Ambil konten halaman web (markdown/selectors/links)",
    description=(
        "Scrape up to 50 URLs with a registered Chrome profile. <=3 URLs and wait_s return "
        "content inline (trimmed); otherwise a job whose result is a handle of pages."
    ),
    input_model=ScrapeIn,
    output_model=ScrapeOut,
    returns_job=True,
    cache_ttl_s=21_600,
    timeout_s=24,
)
async def web_scrape(inp: ScrapeIn, ctx: ToolContext) -> Any:
    params = inp.model_dump(exclude={"wait_s", "response_format", "cache"})
    params["respect_robots"] = _respect_robots(inp.respect_robots, ctx)
    params["urls"] = list(dict.fromkeys(normalize_url(u) for u in inp.urls))  # buang utm & duplikat
    wait = inp.wait_s if len(inp.urls) <= 3 else 0
    return await enqueue_and_wait(
        ctx, "web_scrape", params, wait_s=wait, timeout_s=60 + 30 * len(inp.urls)
    )


class CrawlIn(ToolInput):
    start_url: str
    profile: str = Field(default="default", pattern=PROFILE_PATTERN)
    max_pages: int = Field(default=50, ge=1, le=200)
    max_depth: int = Field(default=2, ge=0, le=3)
    same_domain: bool = True
    include_patterns: list[str] | None = Field(default=None, max_length=20)
    exclude_patterns: list[str] | None = Field(default=None, max_length=20)
    published_since: str | None = None
    published_until: str | None = None
    mode: Literal["text", "markdown"] = "markdown"
    respect_robots: bool = True

    @model_validator(mode="after")
    def _check(self) -> CrawlIn:
        import re

        parse_http_url(self.start_url)
        for pattern in [*(self.include_patterns or []), *(self.exclude_patterns or [])]:
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError(f"regex tidak valid: {pattern}") from None
        return self


@tool(
    name="web_crawl",
    toolset="web",
    summary="Crawl situs BFS (≤200 halaman) -> handle halaman",
    description=(
        "Breadth-first crawl from start_url (max_pages<=200, max_depth<=3) with "
        "include/exclude regex and published date filter. Always a job; result is a handle of "
        "pages + summary stats."
    ),
    input_model=CrawlIn,
    output_model=Envelope,
    returns_job=True,
)
async def web_crawl(inp: CrawlIn, ctx: ToolContext) -> Any:
    params = inp.model_dump()
    params["respect_robots"] = _respect_robots(inp.respect_robots, ctx)
    return await enqueue_and_wait(ctx, "web_crawl", params, timeout_s=60 + 20 * inp.max_pages)


class ExtractSource(BaseModel):
    handle: str | None = Field(default=None, pattern=HANDLE_PATTERN)
    url: str | None = None

    @model_validator(mode="after")
    def _one(self) -> ExtractSource:
        if bool(self.handle) == bool(self.url):
            raise ValueError("isi salah satu: handle atau url")
        if self.url:
            parse_http_url(self.url)
        return self


class ExtractIn(ToolInput):
    source: ExtractSource
    schema_: dict[str, str] = Field(
        alias="schema", min_length=1, max_length=15, description="{field: deskripsi}"
    )
    instructions: str | None = Field(default=None, max_length=1_000)
    max_pages: int = Field(default=5, ge=1, le=20)
    profile: str = Field(default="default", pattern=PROFILE_PATTERN)
    wait_s: int = Field(default=15, ge=0, le=20)

    model_config = {"extra": "forbid", "populate_by_name": True}


@tool(
    name="web_extract",
    toolset="web",
    summary="Ekstrak field terstruktur dari halaman (LLM fast)",
    description=(
        "Extract structured fields ({field: description}, <=15) from a page handle or URL "
        "using the fast LLM route on trimmed markdown (6000 chars/page). Returns a {cols, "
        "rows} table."
    ),
    input_model=ExtractIn,
    output_model=Envelope,
    returns_job=True,
)
async def web_extract(inp: ExtractIn, ctx: ToolContext) -> Any:
    if any(not k.isidentifier() for k in inp.schema_):
        raise ValidationFailed("nama field schema harus identifier (huruf/angka/_)")
    params = {
        "source": inp.source.model_dump(exclude_none=True),
        "schema": inp.schema_,
        "instructions": inp.instructions,
        "max_pages": inp.max_pages,
        "profile": inp.profile,
    }
    result = await enqueue_and_wait(ctx, "web_extract", params, wait_s=inp.wait_s, timeout_s=600)
    if isinstance(result, dict):
        ctx.llm_tokens += int((result.get("cost") or {}).get("llm_tokens") or 0)
    return result
