"""TASK-15/16: parser DOM X & Threads, klien API (mock HTTP), query builder, pemilihan provider."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_marketing.core.errors import (
    BudgetExceeded,
    LoginRequired,
    UpstreamError,
    ValidationFailed,
)
from mcp_marketing.services.collectors import threads_browser, x_browser
from mcp_marketing.services.collectors.social import (
    PostIn,
    compute_stats,
    hashtags_of,
    matched_keywords,
)
from mcp_marketing.services.collectors.threads_api import ThreadsApiClient, ThreadsUnavailable
from mcp_marketing.services.collectors.x_api import XApiClient
from mcp_marketing.services.collectors.x_query import (
    build_query,
    choose_provider,
    estimate_cost,
    validate_period,
    web_query,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


async def no_sleep(_s: float) -> None:
    return None


# ------------------------------------------------------------------ parser DOM
def test_parse_count_formats() -> None:
    assert x_browser.parse_count("1,234 Likes") == 1234
    assert x_browser.parse_count("12 rb Suka") == 12_000
    assert x_browser.parse_count("1,5 rb") == 1_500
    assert x_browser.parse_count("3.4K") == 3_400
    assert x_browser.parse_count("2 jt") == 2_000_000
    assert x_browser.parse_count("") is None


def test_parse_x_search_fixture() -> None:
    posts = x_browser.parse_search_html((FIXTURES / "x_search.html").read_text(encoding="utf-8"))
    assert [p.external_id for p in posts] == [
        "1850000000000000001",
        "1850000000000000002",
        "1850000000000000003",
    ]
    first, repost, reply = posts
    assert first.author_handle == "kopinusantara"
    assert first.posted_at == datetime(2026, 10, 20, 3, 15, tzinfo=UTC)
    assert (first.replies, first.reposts, first.likes, first.views) == (12, 3, 1_500, 25_000)
    assert first.hashtags == ["kopisusu"]
    assert first.lang == "in"
    assert repost.is_repost and repost.likes == 3_400 and repost.reposts == 1_234
    assert reply.is_reply and reply.url == "https://x.com/sari/status/1850000000000000003"


def test_parse_threads_search_fixture() -> None:
    html = (FIXTURES / "threads_search.html").read_text(encoding="utf-8")
    posts = threads_browser.parse_search_html(html)
    assert [p.external_id for p in posts] == ["DAbc123", "DXyz789"]
    first = posts[0]
    assert first.author_handle == "kopinusantara"
    assert first.text.startswith("Menu baru #KopiSusu")
    assert (first.likes, first.replies, first.reposts) == (1_200, 34, 5)
    assert posts[1].url == "https://www.threads.net/@sari/post/DXyz789"


def test_search_urls() -> None:
    assert "f=live" in x_browser.search_url("kopi lang:id")
    assert "serp_type=tags" in threads_browser.search_url("#kopi", "TAG")
    assert x_browser.days_desc(date(2026, 10, 1), date(2026, 10, 3)) == [
        date(2026, 10, 3),
        date(2026, 10, 2),
        date(2026, 10, 1),
    ]


# ------------------------------------------------------------------ browser collector
class FakePage:
    def __init__(self, pages: list[str], url: str = "https://x.com/search") -> None:
        self.pages = pages
        self.idx = 0
        self.url = url
        self.opened: list[str] = []

    async def open(self, url: str, wait_for: str | None = None) -> Any:
        self.opened.append(url)

    async def scroll(self, times: int = 1, pause_s: float = 0) -> None:
        self.idx = min(self.idx + 1, len(self.pages) - 1)

    async def html(self) -> str:
        return self.pages[self.idx]

    async def current_url(self) -> str:
        return self.url

    async def run_js(self, script: str, *args: Any) -> Any:
        return None


async def test_x_browser_stops_after_idle_scrolls() -> None:
    html = (FIXTURES / "x_search.html").read_text(encoding="utf-8")
    page = FakePage([html])
    result = await x_browser.XBrowserCollector(page, sleep=no_sleep).collect_day(
        "kopi", date(2026, 10, 20), quota=100
    )
    assert len(result.posts) == 3
    assert result.scrolls == 3  # berhenti setelah 3 scroll berturut-turut tanpa post baru


async def test_x_browser_login_redirect_raises() -> None:
    page = FakePage(["<html></html>"], url="https://x.com/i/flow/login")
    with pytest.raises(LoginRequired):
        await x_browser.XBrowserCollector(page, sleep=no_sleep).collect_day(
            "kopi", date(2026, 10, 20), quota=10
        )


async def test_threads_browser_filters_period() -> None:
    html = (FIXTURES / "threads_search.html").read_text(encoding="utf-8")
    page = FakePage([html], url="https://www.threads.net/search")
    posts = await threads_browser.ThreadsBrowserCollector(page, sleep=no_sleep).collect(
        "kopi", datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC), 50
    )
    assert [p.external_id for p in posts] == ["DAbc123"]


def test_per_day_quota() -> None:
    assert x_browser.per_day_quota(300, 30) == 15
    assert x_browser.per_day_quota(1, 30) == 1


# ------------------------------------------------------------------ X API
def _tweet(i: int) -> dict[str, Any]:
    return {
        "id": str(i),
        "text": f"post {i} #Kopi",
        "created_at": "2026-10-20T01:00:00.000Z",
        "author_id": "u1",
        "lang": "id",
        "public_metrics": {"like_count": i, "reply_count": 0, "retweet_count": 1},
        "entities": {"hashtags": [{"tag": "Kopi"}]},
    }


def _x_page(ids: range, next_token: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": [_tweet(i) for i in ids],
        "includes": {"users": [{"id": "u1", "username": "kopi", "public_metrics": {}}]},
        "meta": {},
    }
    if next_token:
        body["meta"]["next_token"] = next_token
    return body


async def test_x_api_paginates_three_pages() -> None:
    seen_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("next_token")
        seen_tokens.append(token)
        assert request.headers["Authorization"] == "Bearer tkn"
        pages = {
            None: (range(1, 11), "p2"),
            "p2": (range(11, 21), "p3"),
            "p3": (range(21, 26), None),
        }
        ids, nxt = pages[token]
        return httpx.Response(200, json=_x_page(ids, nxt))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = XApiClient(http, "tkn", sleep=no_sleep)
        pages = [
            page
            async for page in client.search("kopi", NOW - timedelta(days=2), NOW, max_items=100)
        ]
    assert seen_tokens == [None, "p2", "p3"]
    assert sum(len(p) for p, _ in pages) == 25
    assert pages[-1][1] is None
    assert pages[0][0][0].author_handle == "kopi" and pages[0][0][0].hashtags == ["kopi"]


async def test_x_api_waits_for_rate_limit_reset() -> None:
    clock = [1_000.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-rate-limit-reset": "1030"})
        return httpx.Response(200, json=_x_page(range(1, 3), None))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = XApiClient(http, "tkn", sleep=sleep, clock=lambda: clock[0])
        pages = [p async for p in client.search("kopi", NOW - timedelta(days=1), NOW, max_items=10)]
    assert slept == [30.0]
    assert len(pages[0][0]) == 2


@pytest.mark.parametrize("status", [401, 403])
async def test_x_api_auth_errors_are_login_required(status: int) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = XApiClient(http, "bad", sleep=no_sleep)
        with pytest.raises(LoginRequired):
            async for _ in client.search("kopi", NOW - timedelta(days=1), NOW, max_items=10):
                pass


# ------------------------------------------------------------------ Threads API
def _threads_item(code: str) -> dict[str, Any]:
    return {
        "id": code,
        "text": "kopi enak",
        "timestamp": "2026-10-20T01:00:00+0000",
        "username": "sari",
        "permalink": f"https://www.threads.net/@sari/post/{code}",
        "like_count": 3,
    }


async def test_threads_api_follows_cursor_until_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after") == "c2":
            return httpx.Response(200, json={"data": [_threads_item("b")]})
        return httpx.Response(
            200,
            json={
                "data": [_threads_item("a")],
                "paging": {"cursors": {"after": "c2"}, "next": "https://next"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ThreadsApiClient(http, "tkn", sleep=no_sleep)
        pages = [p async for p in client.search("kopi", NOW - timedelta(days=3), NOW)]
    assert [[x.external_id for x in p] for p, _ in pages] == [["a"], ["b"]]
    assert pages[0][1] == "c2" and pages[1][1] is None


async def test_threads_api_empty_result() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = ThreadsApiClient(http, "tkn", sleep=no_sleep)
        pages = [p async for p in client.search("kopi", NOW - timedelta(days=3), NOW)]
    assert pages == [([], None)]


async def test_threads_api_5xx_retries_then_unavailable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ThreadsApiClient(http, "tkn", sleep=no_sleep)
        with pytest.raises(ThreadsUnavailable):
            async for _ in client.search("kopi", NOW - timedelta(days=3), NOW):
                pass
    assert calls["n"] == 4  # 1 + 3 retry


async def test_threads_api_expired_token() -> None:
    body = {"error": {"code": 190, "message": "expired"}}
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json=body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = ThreadsApiClient(http, "tkn", sleep=no_sleep)
        with pytest.raises(LoginRequired):
            async for _ in client.search("kopi", NOW - timedelta(days=3), NOW):
                pass


async def test_threads_api_rejects_since_before_launch() -> None:
    async with httpx.AsyncClient() as http:
        client = ThreadsApiClient(http, "tkn", sleep=no_sleep)
        with pytest.raises(ValidationFailed):
            async for _ in client.search("kopi", datetime(2023, 1, 1, tzinfo=UTC), NOW):
                pass


# ------------------------------------------------------------------ query & provider
def test_build_query_variants() -> None:
    assert build_query(["kopi"]) == "kopi lang:id -is:retweet"
    assert (
        build_query(["kopi susu", "latte"], exclude_replies=True)
        == '("kopi susu" OR latte) lang:id -is:retweet -is:reply'
    )
    assert build_query(["a", "b"], match="all", lang=None, exclude_retweets=False) == "(a b)"
    with pytest.raises(ValidationFailed):
        build_query(["  "])


def test_web_query_uses_since_until_filters() -> None:
    q = web_query(
        ["kopi"],
        match="any",
        lang="id",
        day_start="2026-10-01",
        day_end="2026-10-02",
        exclude_retweets=True,
        exclude_replies=True,
    )
    assert q == "kopi lang:id since:2026-10-01 until:2026-10-02 -filter:retweets -filter:replies"


def test_validate_period_max_366_days() -> None:
    validate_period(NOW - timedelta(days=366), NOW)
    with pytest.raises(ValidationFailed):
        validate_period(NOW - timedelta(days=367), NOW)


def _choose(**over: Any) -> str:
    args: dict[str, Any] = {
        "requested": "auto",
        "has_token": True,
        "start": NOW - timedelta(days=3),
        "now": NOW,
        "estimate_units": 100,
        "remaining_quota": 1_000,
        "browser_ready": True,
    }
    args.update(over)
    return choose_provider(**args).provider


def test_choose_provider_matrix() -> None:
    assert _choose() == "x_api"
    assert _choose(start=NOW - timedelta(days=30)) == "browser"
    assert _choose(has_token=False) == "browser"
    assert _choose(remaining_quota=50) == "browser"
    assert _choose(requested="browser") == "browser"
    with pytest.raises(UpstreamError):
        _choose(has_token=False, browser_ready=False)


def test_choose_provider_explicit_errors() -> None:
    with pytest.raises(ValidationFailed):
        _choose(requested="x_api", start=NOW - timedelta(days=30))
    with pytest.raises(ValidationFailed):
        _choose(requested="x_api", has_token=False)
    with pytest.raises(BudgetExceeded):
        _choose(requested="x_api", remaining_quota=10)
    with pytest.raises(ValidationFailed):
        _choose(requested="browser", browser_ready=False)


def test_estimate_cost() -> None:
    assert estimate_cost(1_000, 0.005) == 5.0


# ------------------------------------------------------------------ util sosial
def test_hashtags_and_keywords() -> None:
    assert hashtags_of("Promo #KopiSusu dan #kopisusu #Latte") == ["kopisusu", "latte"]
    post = PostIn(
        source="x",
        external_id="1",
        text="Kopi Susu enak",
        posted_at=NOW,
        hashtags=["latte"],
    )
    assert matched_keywords(post, ["kopi susu", "teh", "#latte"]) == ["kopi susu", "#latte"]


def test_compute_stats_basic() -> None:
    def post(i: int, day: int, likes: int) -> dict[str, Any]:
        return {
            "posted_at": datetime(2026, 10, day, 5, tzinfo=UTC),
            "likes": likes,
            "replies": 0,
            "reposts": 0,
            "views": None,
            "hashtags": ["kopi"],
            "author": f"u{i % 2}",
        }

    rows = [post(1, 1, 10), post(2, 1, 20), post(3, 2, 30)]
    stats = compute_stats(rows, "Asia/Jakarta")
    assert stats["peak_day"] == {"day": "2026-10-01", "posts": 2}
    assert stats["top_hashtags"] == [["kopi", 3]]
    assert stats["per_day"]["rows"] == [["2026-10-01", 2, 30], ["2026-10-02", 1, 30]]
