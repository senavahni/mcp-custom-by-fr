"""TASK-14–17 terhadap PostgreSQL: lock profil, web_scrape, x_search (dedupe), cache Trends.

Chrome tidak dibuka: launcher palsu disuntik lewat `services.extras["browser_launcher"]`.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from mcp_marketing.core.errors import ProfileBusy
from mcp_marketing.models import Job
from mcp_marketing.models.browser import WebPage
from mcp_marketing.models.social import SocialPost
from mcp_marketing.server.services import Services
from mcp_marketing.services.browser.politeness import DomainRateLimiter
from mcp_marketing.services.browser.profiles import ProfileStore
from mcp_marketing.services.collectors.trends_base import InterestResult
from mcp_marketing.services.collectors.trends_service import TrendsService
from mcp_marketing.utils.time import now
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.runner import Worker
from tests.integration.helpers import FakeLauncher, FakePage, no_sleep

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLIENT = "00000000-0000-0000-0000-0000000000c1"


def _prepare(services: Services, page: FakePage) -> FakeLauncher:
    launcher = FakeLauncher(page)
    services.extras["browser_launcher"] = launcher
    services.extras["sleep"] = no_sleep
    services.extras["domain_limiter"] = DomainRateLimiter(1_000, jitter=(0.0, 0.0), sleep=no_sleep)
    return launcher


async def _run_job(services: Services, kind: str, params: dict[str, Any]) -> Job:
    reg = default_registry()
    queue = JobQueue(services.db, estimator=reg)
    job_id = (await queue.enqueue(kind, params, client_id=CLIENT)).job_id
    worker = Worker(
        services, queue, reg, worker_id="w-test", use_process_pool=False, poll_interval_s=0.05
    )
    assert await worker.run_one()
    async with services.db.session() as s:
        job = await s.get(Job, job_id)
    assert job is not None
    return job


async def test_profile_lock_is_exclusive(services: Services, tmp_path: Path) -> None:
    store = ProfileStore(services.db, tmp_path, sleep=no_sleep)
    await store.add(name="riset", sites=["x.com"])
    await store.acquire("riset", "job_a")
    with pytest.raises(ProfileBusy):
        await store.acquire("riset", "job_b", wait_s=0)
    await store.acquire("riset", "job_a")  # job yang sama boleh mengambil ulang
    await store.release("riset", "job_a")
    await store.acquire("riset", "job_b", wait_s=0)
    await store.set_login_status("riset", {"x.com": {"ok": True}})
    found = await store.for_site("x.com")
    assert found is not None and found.name == "riset"
    assert (tmp_path / "riset").is_dir()


async def test_web_scrape_ok_and_ssrf_denied(services: Services) -> None:
    article = (FIXTURES / "article.html").read_text(encoding="utf-8")
    good = "https://93.184.216.34/artikel/kopi"
    page = FakePage({good: article})
    launcher = _prepare(services, page)
    job = await _run_job(
        services,
        "web_scrape",
        {"urls": [good, "http://127.0.0.1/admin"], "respect_robots": False},
    )
    assert job.status == "succeeded", job.error
    result = job.result or {}
    assert result["stats"]["status"] == {"ok": 1, "ssrf_denied": 1}
    assert page.opened == [good]  # URL internal tidak pernah dibuka
    assert launcher.sessions[0][0] == "default"
    first = result["pages"][0]
    assert first["title"] == "Tren Kopi Susu 2026 - Blog Contoh"
    assert "Kopi susu gula aren" in first["content"]
    assert result["handle"].startswith("h_")


async def test_web_scrape_respects_robots(services: Services) -> None:
    robots = httpx.MockTransport(lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    old_http = services.http
    services.http = httpx.AsyncClient(transport=robots)
    try:
        page = FakePage({})
        _prepare(services, page)
        job = await _run_job(services, "web_scrape", {"urls": ["https://93.184.216.34/x"]})
    finally:
        await services.http.aclose()
        services.http = old_http
    assert (job.result or {})["stats"]["status"] == {"skipped_robots": 1}
    assert page.opened == []


async def test_x_search_browser_dedupes_across_days(services: Services) -> None:
    html = (FIXTURES / "x_search.html").read_text(encoding="utf-8")
    page = FakePage({}, default_html=html)
    _prepare(services, page)
    job = await _run_job(
        services,
        "x_search",
        {
            "keywords": ["kopi"],
            "since": "2026-10-19",
            "until": "2026-10-20",
            "provider": "browser",
            "profile": "default",
            "max_items": 50,
            "lang": None,
            "exclude_retweets": True,
            "days": 2,
        },
    )
    assert job.status == "succeeded", job.error
    assert len(page.opened) == 2  # satu pencarian per hari
    assert all("since%3A" in u for u in page.opened)
    result = job.result or {}
    assert result["total"] == 2  # repost dibuang, post yang sama di dua hari tidak dobel
    assert job.checkpoint == {"days_done": ["2026-10-20", "2026-10-19"]}
    async with services.db.session() as s:
        count = await s.scalar(select(func.count()).select_from(SocialPost))
    assert count == 2


class CountingProvider:
    name = "unofficial"

    def __init__(self) -> None:
        self.calls = 0

    async def interest_over_time(
        self, keywords: list[str], start: date, end: date, geo: str, category: int, gprop: str
    ) -> InterestResult:
        self.calls += 1
        points = [(date(2026, 1, 5), {k: 40 for k in keywords}, False)]
        return InterestResult(keywords, points, "week", "unofficial")


async def test_trends_interest_cached_after_first_call(services: Services) -> None:
    provider = CountingProvider()
    args = (["kopi", "teh"], date(2026, 1, 1), date(2026, 1, 31), "ID", 0, "web")
    async with TrendsService(services, sleep=no_sleep, providers={"unofficial": provider}) as svc:
        first = await svc.interest(*args)
        second = await svc.interest(*args)
    assert provider.calls == 1
    assert not first.cache_hit and second.cache_hit
    assert second.interest is not None
    assert second.interest.points[0][1] == {"kopi": 40, "teh": 40}


async def test_retention_deletes_old_pages(services: Services) -> None:
    old = now() - timedelta(days=services.settings.worker.social_retention_days + 1)
    async with services.db.session() as s:
        s.add(WebPage(url="https://a.test/old", domain="a.test", status="ok", fetched_at=old))
        s.add(WebPage(url="https://a.test/new", domain="a.test", status="ok"))
    job = await _run_job(services, "retention", {})
    assert (job.result or {})["stats"]["web_pages"] == 1
    async with services.db.session() as s:
        urls = (await s.scalars(select(WebPage.url))).all()
    assert urls == ["https://a.test/new"]
