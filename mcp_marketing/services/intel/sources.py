"""Persiapan job anak tahap Kumpulkan per sumber `intel` (aturan sama dengan tool x_search dkk.)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from mcp_marketing.core.errors import SourceUnavailable, ToolError
from mcp_marketing.schemas.intel import Period, ThreadsSource, TrendsSource, WebSource, XSource
from mcp_marketing.services.browser.driver_pool import profile_store
from mcp_marketing.services.collectors.x_query import choose_provider, estimate_cost
from mcp_marketing.utils.time import now, parse_period
from mcp_marketing.worker.handlers.social import x_reads_today
from mcp_marketing.worker.handlers.web import normalize_url

DEFAULT_PROFILE = "default"
GOOGLE_SITE = "trends.google.com"  # kunci SITE_CHECKS untuk akun Google
SECONDS_PER_WEB_PAGE = 6
SECONDS_PER_CRAWL_PAGE = 5
TRENDS_SECONDS = 35
TRENDS_MODE = {"unofficial": "unofficial", "browser": "browser", "official": "api"}


@dataclass
class PreparedSource:
    type: str
    kind: str  # kind handler job anak
    params: dict[str, Any]
    mode: str  # api | browser | unofficial | none
    expected_items: int  # perkiraan jumlah teks untuk analisis
    seconds: int
    timeout_s: int
    external_units: int = 0
    usd_external: float = 0.0
    warnings: list[dict[str, str]] = field(default_factory=list)


async def prepare_source(services: Any, src: Any, period: Period) -> PreparedSource:
    """Tentukan provider & parameter job anak; `SOURCE_UNAVAILABLE` bila sumber tidak siap."""
    try:
        if isinstance(src, XSource):
            return await _x(services, src, period)
        if isinstance(src, ThreadsSource):
            return await _threads(services, src, period)
        if isinstance(src, WebSource):
            return _web(src, await web_profile(services))
        assert isinstance(src, TrendsSource)
        return _trends(services, src, period)
    except SourceUnavailable:
        raise
    except ToolError as exc:
        raise SourceUnavailable(
            f"Sumber {src.type} tidak tersedia: {exc.message}",
            details={"source": src.type, "reason": exc.code},
        ) from None


async def _x(services: Any, src: XSource, period: Period) -> PreparedSource:
    s = services.settings
    start, end = parse_period(period.since.isoformat(), period.until.isoformat(), s.timezone)
    found = await profile_store(services).for_site("x.com")
    remaining = max(0, s.social.x_max_reads_per_day - await x_reads_today(services))
    decision = choose_provider(
        requested=src.provider,
        has_token=s.social.x_bearer_token is not None,
        start=start,
        now=now(),
        estimate_units=src.max_items,
        remaining_quota=remaining,
        browser_ready=found is not None,
    )
    tz = ZoneInfo(s.timezone)
    days = (end.astimezone(tz).date() - start.astimezone(tz).date()).days + 1
    params = {
        "keywords": list(src.keywords),
        "match": src.match,
        "since": period.since.isoformat(),
        "until": period.until.isoformat(),
        "lang": src.lang,
        "exclude_retweets": src.exclude_retweets,
        "exclude_replies": src.exclude_replies,
        "min_likes": None,
        "max_items": src.max_items,
        "provider": decision.provider,
        "profile": found.name if found else None,
        "days": days,
        "price_per_read": s.social.x_price_per_read_usd,
    }
    warnings = []
    if decision.provider == "browser" and s.social.x_bearer_token is not None:
        warnings.append(
            {
                "code": "X_API_WINDOW",
                "message": "X API hanya 7 hari terakhir / kuota habis; memakai mode browser",
            }
        )
    if decision.provider == "x_api":
        usd = estimate_cost(src.max_items, s.social.x_price_per_read_usd)
        return PreparedSource(
            "x", "x_search", params, "api", src.max_items, max(10, src.max_items // 50),
            3_600, external_units=src.max_items, usd_external=usd, warnings=warnings,
        )  # fmt: skip
    return PreparedSource(
        "x", "x_search", params, "browser", src.max_items, days * 60 + src.max_items // 10,
        4 * 3_600, warnings=warnings,
    )  # fmt: skip


async def _threads(services: Any, src: ThreadsSource, period: Period) -> PreparedSource:
    s = services.settings
    browser = await profile_store(services).for_site("threads.net")
    has_token = s.social.threads_access_token is not None
    if not has_token and browser is None:
        raise SourceUnavailable(
            "Threads tidak tersedia: isi THREADS_ACCESS_TOKEN atau siapkan profil browser login "
            "threads.net",
            details={"source": "threads"},
        )
    provider = "threads_api" if has_token else "browser"
    params = {
        "keywords": list(src.keywords),
        "since": period.since.isoformat(),
        "until": period.until.isoformat(),
        "search_type": src.search_type,
        "search_mode": "KEYWORD",
        "media_type": None,
        "max_items": src.max_items,
        "provider": provider,
        "profile": browser.name if browser else None,
        "fallback_browser": has_token and browser is not None,
    }
    per_kw = 5 if provider == "threads_api" else 60
    seconds = len(src.keywords) * per_kw + src.max_items // 20
    mode = "api" if has_token else "browser"
    return PreparedSource(
        "threads", "threads_search", params, mode, src.max_items, seconds, 3 * 3_600
    )


async def web_profile(services: Any) -> str:
    """Profil yang sudah login Google (lebih mirip pengguna nyata) bila ada; selain itu default."""
    found = await profile_store(services).for_site(GOOGLE_SITE)
    return found.name if found else DEFAULT_PROFILE


def _web(src: WebSource, profile: str = DEFAULT_PROFILE) -> PreparedSource:
    if src.mode == "scrape":
        urls = list(dict.fromkeys(normalize_url(u) for u in src.urls or []))
        params: dict[str, Any] = {
            "urls": urls,
            "profile": profile,
            "mode": "markdown",
            "selectors": None,
            "wait_for": None,
            "scroll": 0,
            "max_chars_per_page": 20_000,
            "respect_robots": True,
        }
        seconds = SECONDS_PER_WEB_PAGE * len(urls) + 5
        return PreparedSource(
            "web", "web_scrape", params, "browser", len(urls), seconds, 60 + 30 * len(urls)
        )
    params = {
        "start_url": src.start_url,
        "profile": profile,
        "max_pages": src.max_pages,
        "max_depth": src.max_depth,
        "same_domain": True,
        "include_patterns": list(src.include_patterns) or None,
        "exclude_patterns": None,
        "published_since": None,
        "published_until": None,
        "mode": "markdown",
        "respect_robots": True,
    }
    seconds = SECONDS_PER_CRAWL_PAGE * src.max_pages
    return PreparedSource(
        "web", "web_crawl", params, "browser", src.max_pages, seconds, 60 + 20 * src.max_pages
    )


def _trends(services: Any, src: TrendsSource, period: Period) -> PreparedSource:
    end = min(period.until, date.today())
    params = {
        "keywords": list(src.keywords),
        "start": period.since.isoformat(),
        "end": max(end, period.since).isoformat(),
        "geo": src.geo,
        "category": 0,
        "gprop": "web",
        "anchor": None,
        "compare_previous": True,
    }
    mode = TRENDS_MODE[services.settings.trends.trends_provider]
    return PreparedSource("trends", "trends_interest", params, mode, 0, TRENDS_SECONDS, 600)
