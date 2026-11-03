"""TASK-17: parser Google Trends (JSON widget & CSV), statistik, klien, retry/fallback provider."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.analysis import trend_stats
from mcp_marketing.services.collectors import trends_service
from mcp_marketing.services.collectors.trends_base import (
    BREAKOUT_VALUE,
    InterestResult,
    TrendsRateLimited,
    parse_geo,
    parse_multiline,
    parse_related,
    parse_trends_csv,
    strip_xssi,
)
from mcp_marketing.services.collectors.trends_google import GoogleTrendsClient
from mcp_marketing.services.collectors.trends_service import (
    TrendsService,
    params_hash,
    previous_period,
)


async def no_sleep(_s: float) -> None:
    return None


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def test_strip_xssi_and_multiline() -> None:
    body = strip_xssi(
        ")]}',\n"
        + orjson.dumps(
            {
                "default": {
                    "timelineData": [
                        {"time": str(_ts(date(2026, 1, 4))), "value": [10, 50]},
                        {"time": str(_ts(date(2026, 1, 11))), "value": [20, 0], "isPartial": True},
                    ]
                }
            }
        ).decode()
    )
    res = parse_multiline(body, ["kopi", "teh"])
    assert res.resolution == "week"
    assert res.points[1] == (date(2026, 1, 11), {"kopi": 20, "teh": 0}, True)


def test_strip_xssi_rejects_html() -> None:
    with pytest.raises(UpstreamError):
        strip_xssi("<html>blocked</html>")


def test_parse_geo_and_related() -> None:
    geo = parse_geo(
        {
            "default": {
                "geoMapData": [
                    {"geoName": "Jawa Barat", "geoCode": "ID-JB", "value": [80], "hasData": [True]},
                    {"geoName": "Bali", "geoCode": "ID-BA", "value": [100], "hasData": [True]},
                ]
            }
        }
    )
    assert [r.region for r in geo] == ["Bali", "Jawa Barat"]
    rel = parse_related(
        {
            "default": {
                "rankedList": [
                    {"rankedKeyword": [{"query": "kopi susu", "value": 100}]},
                    {
                        "rankedKeyword": [
                            {"query": "kopi pandan", "value": 250, "formattedValue": "Breakout"}
                        ]
                    },
                ]
            }
        },
        "queries",
    )
    assert rel.top[0].term == "kopi susu" and rel.top[0].position == 1
    assert rel.rising[0].value == BREAKOUT_VALUE


def test_parse_trends_csv_interest_id_labels() -> None:
    text = (
        "Kategori: Semua kategori\n\n"
        "Minggu,kopi: (Indonesia),teh: (Indonesia)\n"
        "2026-01-04,10,<1\n"
        "2026-01-11,20,5\n"
    )
    parsed = parse_trends_csv(text)
    assert parsed["kind"] == "interest"
    res = parsed["result"]
    assert res.keywords == ["kopi", "teh"]
    assert res.points[0][1] == {"kopi": 10, "teh": 0}


def test_parse_trends_csv_region_and_related() -> None:
    region = parse_trends_csv("Wilayah,kopi: (1/1/26 - 1/2/26)\nBali,100\nAceh,\n")
    assert region["kind"] == "region"
    assert [(r.region, r.value) for r in region["result"]] == [("Bali", 100), ("Aceh", 0)]
    related = parse_trends_csv("TERATAS\nkopi susu,100\nMENINGKAT\nkopi pandan,Lonjakan\n")
    assert related["result"].top[0].term == "kopi susu"
    assert related["result"].rising[0].value == BREAKOUT_VALUE


def _weekly(values: list[int], start: date = date(2026, 1, 5)) -> list[tuple[date, int, bool]]:
    return [(start + timedelta(weeks=i), v, False) for i, v in enumerate(values)]


def test_keyword_stats_synthetic() -> None:
    series = _weekly([10, 20, 30, 40])
    stats = trend_stats.keyword_stats(series, _weekly([10, 10, 10, 10]))
    assert stats["avg"] == 25.0
    assert stats["peak"] == {"date": "2026-01-26", "value": 40}
    assert stats["slope_per_week"] == pytest.approx(10.0)
    assert stats["change_pct_vs_prev_period"] == 150.0
    assert "seasonality_hint" not in stats


def test_keyword_stats_seasonality_and_flat() -> None:
    start = date(2024, 1, 1)
    long_series = [
        (start + timedelta(weeks=i), 90 if (start + timedelta(weeks=i)).month == 12 else 10, False)
        for i in range(110)
    ]
    assert trend_stats.keyword_stats(long_series, None)["seasonality_hint"] == "December"
    flat = trend_stats.keyword_stats(_weekly([5, 5, 5]), None)
    assert flat["slope_per_week"] == 0.0


def test_summarize_share_and_resample() -> None:
    points = [(date(2026, 1, 1) + timedelta(days=i), {"a": 30, "b": 10}, False) for i in range(400)]
    res = InterestResult(["a", "b"], points, "day")
    summary = trend_stats.summarize(res)
    assert summary["share_of_search"] == {"a": 75.0, "b": 25.0}
    table, granularity = trend_stats.resample_table(res)
    assert granularity == "month" and len(table["rows"]) <= 26
    short = InterestResult(["a"], [(date(2026, 1, 5), {"a": 1}, False)], "week")
    assert trend_stats.resample_table(short)[1] == "week"


def test_scale_with_anchor() -> None:
    d = date(2026, 1, 5)
    g1 = InterestResult(["anchor", "a"], [(d, {"anchor": 50, "a": 20}, False)], "week", "x")
    g2 = InterestResult(["anchor", "b"], [(d, {"anchor": 25, "b": 40}, False)], "week", "x")
    merged = trend_stats.scale_with_anchor([g1, g2], "anchor")
    assert merged.keywords == ["anchor", "a", "b"]
    assert merged.points[0][1] == {"anchor": 50, "a": 20, "b": 80}


def test_previous_period_and_params_hash() -> None:
    assert previous_period(date(2026, 2, 1), date(2026, 2, 28)) == (
        date(2026, 1, 4),
        date(2026, 1, 31),
    )
    a = params_hash("related", keyword="kopi", kind="queries")
    assert a == params_hash("related", kind="queries", keyword="kopi")
    assert a != params_hash("interest", keyword="kopi", kind="queries")


class FakeTransport:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def get_text(self, path: str, params: dict[str, Any]) -> str:
        self.paths.append(path)
        if path.endswith("/explore"):
            widgets = [{"id": "TIMESERIES", "request": {"x": 1}, "token": "t"}]
            return ")]}'\n" + orjson.dumps({"widgets": widgets}).decode()
        body = {"default": {"timelineData": [{"time": str(_ts(date(2026, 1, 4))), "value": [7]}]}}
        return ")]}',\n" + orjson.dumps(body).decode()


async def test_google_client_explore_then_widget_with_min_interval() -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    transport = FakeTransport()
    client = GoogleTrendsClient(transport, name="unofficial", min_interval_s=5.0, sleep=sleep)
    res = await client.interest_over_time(
        ["kopi"], date(2026, 1, 1), date(2026, 1, 31), "ID", 0, "web"
    )
    assert transport.paths == ["/trends/api/explore", "/trends/api/widgetdata/multiline"]
    assert slept == [5.0]
    assert res.points[0][1] == {"kopi": 7}


class FlakyProvider:
    def __init__(self, name: str, fail_times: int) -> None:
        self.name = name
        self.fail_times = fail_times
        self.calls = 0

    async def interest_over_time(self, keywords: list[str], *_a: Any) -> InterestResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TrendsRateLimited("429")
        return InterestResult(keywords, [], None, self.name)


def _service(providers: dict[str, Any], provider: str = "unofficial") -> TrendsService:
    settings = SimpleNamespace(
        trends=SimpleNamespace(trends_provider=provider, trends_cache_ttl_s=86_400)
    )
    services = SimpleNamespace(settings=settings, db=None, extras={})
    return TrendsService(services, sleep=no_sleep, providers=providers)


async def test_call_retries_429_with_backoff_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trends_service.random, "uniform", lambda a, b: 0.0)
    flaky = FlakyProvider("unofficial", fail_times=2)
    svc = _service({"unofficial": flaky, "browser": FlakyProvider("browser", 0)})
    _, name = await svc._call(lambda p: p.interest_over_time(["kopi"]))
    assert name == "unofficial" and flaky.calls == 3


async def test_call_falls_back_after_repeated_429() -> None:
    unofficial = FlakyProvider("unofficial", fail_times=99)
    browser = FlakyProvider("browser", fail_times=0)
    svc = _service({"unofficial": unofficial, "browser": browser})
    result, name = await svc._call(lambda p: p.interest_over_time(["kopi"]))
    assert name == "browser" and result.provider == "browser"
    assert unofficial.calls == 4  # 1 + 3 backoff


async def test_call_all_providers_fail() -> None:
    svc = _service({"unofficial": FlakyProvider("u", 99), "browser": FlakyProvider("b", 99)})
    with pytest.raises(UpstreamError) as err:
        await svc._call(lambda p: p.interest_over_time(["kopi"]))
    assert err.value.retryable
