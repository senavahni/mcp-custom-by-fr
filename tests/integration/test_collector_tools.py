"""TASK-14–17 end-to-end lewat pipeline tool + worker sungguhan (Chrome & API eksternal dipalsukan)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import func, select

from mcp_marketing.models import Job
from mcp_marketing.server.app import build_asgi_app
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.pipeline import ToolPipeline
from mcp_marketing.server.services import Services
from mcp_marketing.services.browser.politeness import DomainRateLimiter
from mcp_marketing.services.browser.profiles import ProfileStore
from mcp_marketing.worker.handlers import default_registry
from mcp_marketing.worker.queue import JobQueue
from mcp_marketing.worker.runner import Worker
from tests.integration.conftest import make_client
from tests.integration.helpers import FakeLauncher, FakePage, no_sleep

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
X_HOME_HTML = '<html><a data-testid="AppTabBar_Home_Link" href="/home"></a></html>'
THREADS_HOME_HTML = '<html><a href="/activity"></a></html>'


@dataclass
class Env:
    services: Services
    pipeline: ToolPipeline
    page: FakePage
    profiles: ProfileStore
    client: ClientIdentity

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        return await self.pipeline.call(tool, args, client=self.client)

    def use_http(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.services.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def use_settings(self, **social: Any) -> None:
        s = self.services.settings
        self.services.settings = s.model_copy(update={"social": s.social.model_copy(update=social)})


@pytest_asyncio.fixture
async def env(services: Services, tmp_path: Path) -> AsyncIterator[Env]:
    reg = default_registry()
    services.jobs = JobQueue(services.db, estimator=reg)
    page = FakePage({})
    profiles = ProfileStore(services.db, tmp_path, sleep=no_sleep)
    await profiles.ensure_default()
    services.extras.update(
        browser_launcher=FakeLauncher(page),
        profile_store=profiles,
        sleep=no_sleep,
        domain_limiter=DomainRateLimiter(1_000, jitter=(0.0, 0.0), sleep=no_sleep),
    )
    original_http, original_settings = services.http, services.settings
    worker = Worker(
        services,
        services.jobs,
        reg,
        worker_id="w-e2e",
        use_process_pool=False,
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(worker.run())
    _, pipeline, _ = build_asgi_app(services)
    _, client = await make_client(
        services, toolsets=["web", "social", "trends", "job"], is_admin=True, rpm_limit=10_000
    )
    yield Env(services, pipeline, page, profiles, client)
    worker.stop()
    await task
    await pipeline.drain()
    if services.http is not original_http:
        await services.http.aclose()
    services.http, services.settings = original_http, original_settings


async def _job_count(services: Services, kind: str) -> int:
    async with services.db.session() as s:
        return int(
            await s.scalar(select(func.count()).select_from(Job).where(Job.kind == kind)) or 0
        )


# ------------------------------------------------------------------ TASK-14
async def test_web_tools_scrape_crawl_and_profiles(env: Env) -> None:
    article = (FIXTURES / "article.html").read_text(encoding="utf-8")
    base = "https://93.184.216.34"
    env.page.html_by_url.update(
        {
            f"{base}/": f'<html><title>Home</title><a href="{base}/artikel/1">a</a>'
            f'<a href="{base}/login">x</a><a href="https://other.test/">o</a></html>',
            f"{base}/artikel/1": article,
        }
    )
    env.use_http(lambda r: httpx.Response(404))  # robots.txt tidak ada -> boleh

    listed = (await env.call("browser_profile_list", {})).structured
    assert [row[0] for row in listed["items"]["rows"]] == ["default"]

    scraped = (
        await env.call("web_scrape", {"urls": [f"{base}/artikel/1?utm_source=x"], "wait_s": 15})
    ).structured
    assert scraped["stats"]["status"] == {"ok": 1}
    assert "Kopi susu gula aren" in scraped["pages"][0]["content"]

    accepted = (
        await env.call(
            "web_crawl",
            {
                "start_url": f"{base}/",
                "max_pages": 5,
                "max_depth": 1,
                "exclude_patterns": ["/login"],
            },
        )
    ).structured
    waited = (
        await env.call("job_wait", {"job_id": accepted["job_id"], "timeout_s": 20})
    ).structured
    assert waited["status"] == "succeeded"
    crawled = waited["result"]
    assert crawled["total"] == 2  # home + artikel; /login dan domain lain dilewati
    assert f"{base}/login" not in env.page.opened


async def test_browser_profile_check_then_x_search_browser_and_stats(env: Env) -> None:
    await env.profiles.add(name="riset-x", sites=["x.com"])
    env.page.html_by_url["https://x.com/home"] = X_HOME_HTML
    check = (
        await env.call("browser_profile_check", {"profile": "riset-x", "wait_s": 15})
    ).structured
    assert check["checks"] == [
        {"site": "x.com", "logged_in": True, "detail": "penanda akun terdeteksi"}
    ]

    env.page.default_html = (FIXTURES / "x_search.html").read_text(encoding="utf-8")
    result = await env.call(
        "x_search",
        {
            "keywords": ["kopi"],
            "since": "2026-10-19",
            "until": "2026-10-20",
            "lang": None,
            "provider": "auto",
            "wait_s": 20,
        },
    )
    out = result.structured
    assert out["total"] == 2 and out["handle"].startswith("h_")

    stats = (
        await env.call(
            "social_stats",
            {"handle": out["handle"], "group_by": "keyword", "metrics": ["posts", "likes"]},
        )
    ).structured
    assert stats["share_of_voice"] == {"kopi": 100.0}
    by_day = (await env.call("social_stats", {"handle": out["handle"]})).structured
    assert by_day["cols"] == ["day", "posts", "likes"]


def _x_api_handler(request: httpx.Request) -> httpx.Response:
    tweets = [
        {
            "id": str(i),
            "text": f"kopi enak {i}",
            "created_at": (datetime.now(UTC) - timedelta(hours=i)).isoformat(),
            "author_id": "u1",
            "public_metrics": {"like_count": i},
        }
        for i in range(1, 4)
    ]
    body = {"data": tweets, "includes": {"users": [{"id": "u1", "username": "kopi"}]}}
    return httpx.Response(200, json=body)


async def test_x_search_x_api_counts_external_units(env: Env) -> None:
    env.use_settings(x_bearer_token=SecretStr("tkn"))
    env.use_http(_x_api_handler)
    today = date.today()
    out = (
        await env.call(
            "x_search",
            {
                "keywords": ["kopi"],
                "since": (today - timedelta(days=1)).isoformat(),
                "until": today.isoformat(),
                "lang": None,
                "max_items": 50,
                "wait_s": 20,
            },
        )
    ).structured
    assert out["total"] == 3
    assert out["cost"]["external_units"] == 3

    over = await env.call(
        "x_search",
        {
            "keywords": ["teh"],
            "since": today.isoformat(),
            "until": today.isoformat(),
            "provider": "x_api",
            "max_items": 5_000,
        },
    )
    assert over.error_code == "BUDGET_EXCEEDED"


# ------------------------------------------------------------------ TASK-16
async def test_threads_api_5xx_falls_back_to_browser(env: Env) -> None:
    env.use_settings(threads_access_token=SecretStr("tkn"))
    env.use_http(lambda r: httpx.Response(500))
    await env.profiles.add(name="riset-th", sites=["threads.net"])
    await env.profiles.set_login_status("riset-th", {"threads.net": {"ok": True}})
    env.page.default_html = (FIXTURES / "threads_search.html").read_text(encoding="utf-8")
    out = (
        await env.call(
            "threads_search",
            {"keywords": ["kopi"], "since": "2026-10-18", "until": "2026-10-21", "wait_s": 20},
        )
    ).structured
    assert out["total"] == 2
    assert "threads_api_5xx_fallback_browser" in out["stats"]["notes"]


async def test_threads_without_token_or_profile_is_validation_error(env: Env) -> None:
    env.use_settings(threads_access_token=None)
    res = await env.call(
        "threads_search", {"keywords": ["kopi"], "since": "2026-10-18", "until": "2026-10-21"}
    )
    assert res.error_code == "VALIDATION_ERROR"


# ------------------------------------------------------------------ TASK-17
def _trends_body(request: httpx.Request) -> dict[str, Any]:
    path = request.url.path
    if path.endswith("/explore"):
        return {
            "widgets": [
                {"id": wid, "request": {"w": wid}, "token": f"t-{wid}"}
                for wid in ("TIMESERIES", "GEO_MAP", "RELATED_QUERIES", "RELATED_TOPICS")
            ]
        }
    if path.endswith("/multiline"):
        start = date(2026, 1, 5)
        rows = [
            {
                "time": str(
                    int(
                        datetime.combine(
                            start + timedelta(weeks=i), datetime.min.time(), tzinfo=UTC
                        ).timestamp()
                    )
                ),
                "value": [10 + i, 5],
            }
            for i in range(8)
        ]
        return {"default": {"timelineData": rows}}
    if path.endswith("/comparedgeo"):
        return {
            "default": {
                "geoMapData": [
                    {"geoName": "Bali", "geoCode": "ID-BA", "value": [100]},
                    {"geoName": "Aceh", "geoCode": "ID-AC", "value": [40]},
                ]
            }
        }
    return {
        "default": {
            "rankedList": [
                {
                    "rankedKeyword": [
                        {"query": "kopi susu", "value": 100, "topic": {"title": "Kopi"}}
                    ]
                },
                {
                    "rankedKeyword": [
                        {
                            "query": "kopi pandan",
                            "value": 300,
                            "formattedValue": "Breakout",
                            "topic": {"title": "Pandan"},
                        }
                    ]
                },
            ]
        }
    }


async def test_trends_tools_job_then_cache(env: Env) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "/trends/api/" not in request.url.path:
            return httpx.Response(200, text="<html></html>")  # warm-up cookie
        return httpx.Response(200, text=")]}',\n" + orjson.dumps(_trends_body(request)).decode())

    env.use_http(handler)
    args = {"keywords": ["kopi", "teh"], "start": "2026-01-05", "end": "2026-03-01", "wait_s": 20}
    first = (await env.call("trends_interest", args)).structured
    assert set(first["stats"]["per_keyword"]) == {"kopi", "teh"}
    api_calls = sum("/trends/api/" in c for c in calls)
    second = (await env.call("trends_interest", args)).structured
    assert sum("/trends/api/" in c for c in calls) == api_calls  # cache: tanpa panggilan Google
    assert second["stats"]["per_keyword"] == first["stats"]["per_keyword"]
    assert await _job_count(env.services, "trends_interest") == 1

    region = (
        await env.call(
            "trends_by_region",
            {
                "keyword": "kopi",
                "start": "2026-01-05",
                "end": "2026-03-01",
                "top_n": 5,
                "wait_s": 20,
            },
        )
    ).structured
    assert region["rows"][0][0] == "Bali"

    related = (
        await env.call(
            "trends_related",
            {"keyword": "kopi", "start": "2026-01-05", "end": "2026-03-01", "wait_s": 20},
        )
    ).structured
    assert related["rising"]["rows"][0][:2] == ["kopi pandan", 5000]
    again = (
        await env.call(
            "trends_related",
            {"keyword": "kopi", "start": "2026-01-05", "end": "2026-03-01", "wait_s": 20},
        )
    ).structured
    assert again["cache_hit"] is True


async def test_trends_interest_validation(env: Env) -> None:
    six = {"keywords": list("abcdef"), "start": "2026-01-05", "end": "2026-03-01"}
    assert (await env.call("trends_interest", six)).error_code == "VALIDATION_ERROR"
    bad = {"keywords": ["kopi"], "start": "2026-03-01", "end": "2026-01-01"}
    assert (await env.call("trends_interest", bad)).error_code == "VALIDATION_ERROR"
