"""MCP-V1-06: parser Google Search/Maps (fixture HTML), deteksi CAPTCHA, rem volume, kolektor."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mcp_marketing.core.config import DiscoverySettings
from mcp_marketing.core.errors import CaptchaRequired, RateLimited
from mcp_marketing.services.browser.driver_pool import FetchedPage
from mcp_marketing.services.collectors import google_discovery as gd

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _settings(**over: Any) -> DiscoverySettings:
    base: dict[str, Any] = {"discovery_pause_min_s": 0, "discovery_pause_max_s": 0}
    base.update(over)
    return DiscoverySettings(**base)


# ---------------------------------------------------------------------------- Search


def test_parse_search_keeps_organic_unique_company_domains() -> None:
    results = gd.parse_search_html(_fx("google_search.html"))
    assert [r.domain for r in results] == [
        "smakbpk1.sch.id",
        "santaangela.sch.id",
        "aloysius.sch.id",
    ]
    assert [r.rank for r in results] == [1, 2, 3]
    assert results[0].title == "SMAK BPK Penabur 1 Bandung"
    assert results[0].snippet and "Sekolah Menengah Atas" in results[0].snippet
    # tautan /url?q= dibuka; snippet gaya lama (.st) tetap terbaca
    assert results[1].url == "https://santaangela.sch.id/profil"
    assert results[2].snippet == "Website resmi SMA Aloysius."


def test_parse_search_drops_ads_marketplace_directory_social() -> None:
    domains = {r.domain for r in gd.parse_search_html(_fx("google_search.html"))}
    for bad in (
        "iklan-sekolah.example",
        "iklan-bawah.example",
        "tokopedia.com",
        "instagram.com",
        "id.wikipedia.org",
        "referensi.data.kemdikbud.go.id",
        "google.com",
    ):
        assert bad not in domains


def test_blocklist_extra_from_settings_and_subdomains() -> None:
    blocked = gd.blocked_domains(_settings(discovery_blocked_domains="aloysius.sch.id, X.ORG"))
    assert "x.org" in blocked
    assert gd.is_blocked("m.facebook.com", blocked)
    assert not gd.is_blocked("notfacebook.com", blocked)
    results = gd.parse_search_html(_fx("google_search.html"), blocked)
    assert "aloysius.sch.id" not in {r.domain for r in results}


def test_parse_search_without_results_root() -> None:
    assert gd.parse_search_html("<html><body>kosong</body></html>") == []


def test_search_url_pages() -> None:
    assert gd.search_url("sekolah bandung") == (
        "https://www.google.com/search?q=sekolah+bandung&hl=id&gl=id"
    )
    assert gd.search_url("a", 1).endswith("&start=10")


# ---------------------------------------------------------------------------- Maps


def test_parse_maps_feed_dedupes_and_reads_coordinates() -> None:
    feed = gd.parse_maps_feed(_fx("google_maps_feed.html"))
    assert [p.name for p in feed] == ["Rumah Sakit Santo Borromeus", "RSUP Dr. Hasan Sadikin"]
    assert (feed[0].lat, feed[0].lng) == (-6.8932, 107.6129)
    assert gd.parse_maps_feed("<html></html>") == []


def test_parse_maps_place_listing_without_reviews() -> None:
    url = "https://www.google.com/maps/place/RS/@-6.89,107.61,17z/data=!3d-6.8932!4d107.6129"
    listing = gd.parse_maps_place(_fx("google_maps_place.html"), url)
    assert listing is not None
    assert listing.name == "Rumah Sakit Santo Borromeus"
    assert listing.category == "Rumah sakit swasta"
    assert listing.address and listing.address.startswith("Jl. Ir. H. Juanda No.100")
    assert "Alamat:" not in listing.address
    assert listing.phone == "+62222552000"
    assert listing.website == "https://www.rsborromeus.com/"
    assert (listing.lat, listing.lng) == (-6.8932, 107.6129)  # !3d!4d mengalahkan @
    assert listing.rating == 4.6
    assert listing.review_count == 12345
    assert not hasattr(listing, "reviews")  # isi ulasan & pengulas tidak diambil


def test_parse_maps_place_without_name() -> None:
    assert gd.parse_maps_place("<html><h1></h1></html>", "https://maps") is None


def test_coords_from_url_variants() -> None:
    assert gd.coords_from_url("https://maps/@-6.9,107.6,15z") == (-6.9, 107.6)
    assert gd.coords_from_url("https://maps/place/x") == (None, None)
    assert gd.coords_from_url("https://maps/@-99.1,107.6,15z") == (None, None)


# ---------------------------------------------------------------------------- blokir


def test_detect_block() -> None:
    assert gd.detect_block("https://www.google.com/sorry/index?continue=x", "") == "captcha"
    assert (
        gd.detect_block("https://www.google.com/search?q=x", _fx("google_sorry.html")) == "captcha"
    )
    assert gd.detect_block("https://consent.google.com/m?x", "") == "consent"
    assert gd.detect_block("https://www.google.com/search?q=x", _fx("google_search.html")) is None


# ---------------------------------------------------------------------------- rem volume


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


async def test_brake_hourly_limit_waits_for_window() -> None:
    clock = FakeClock()
    brake = gd.VolumeBrake(_settings(discovery_search_per_hour=2), sleep=clock.sleep, clock=clock)
    assert await brake.acquire("google_search") == 0
    clock.t += 10
    assert await brake.acquire("google_search") == 0
    assert brake.remaining("google_search")["hour"] == 0
    waited = await brake.acquire("google_search")
    assert waited == pytest.approx(3_590)
    assert brake.remaining("google_maps")["hour"] == 30  # jenis lain tidak terpengaruh


async def test_brake_random_pause_between_navigations() -> None:
    clock = FakeClock()
    brake = gd.VolumeBrake(
        _settings(discovery_pause_min_s=5, discovery_pause_max_s=5), sleep=clock.sleep, clock=clock
    )
    await brake.acquire("google_maps")
    clock.t += 2
    assert await brake.acquire("google_search") == pytest.approx(3)


async def test_brake_daily_quota_raises_and_resets_next_day() -> None:
    clock = FakeClock()
    day = [date(2026, 11, 9)]
    brake = gd.VolumeBrake(
        _settings(discovery_maps_per_day=1), sleep=clock.sleep, clock=clock, today=lambda: day[0]
    )
    await brake.acquire("google_maps")
    with pytest.raises(RateLimited) as exc:
        await brake.acquire("google_maps")
    assert exc.value.retryable
    day[0] = date(2026, 11, 10)
    await brake.acquire("google_maps")


# ---------------------------------------------------------------------------- kolektor


class FakePage:
    def __init__(self, pages: dict[str, tuple[str, str]]) -> None:
        self.pages = pages  # url -> (final_url, html)
        self.opened: list[str] = []
        self.current = ""

    async def open(self, url: str, *, wait_for: str | None = None) -> FetchedPage:
        self.opened.append(url)
        final, html = self.pages.get(url, (url, "<html></html>"))
        self.current = html
        return FetchedPage(url=url, final_url=final, status=200, title=None, html=html)

    async def scroll(self, times: int = 1, pause_s: float = 1.0) -> None: ...

    async def html(self) -> str:
        return self.current

    async def current_url(self) -> str:
        return ""

    async def run_js(self, script: str, *args: Any) -> Any:
        return None


def _collector(page: FakePage) -> tuple[gd.GoogleDiscovery, FakeClock]:
    clock = FakeClock()
    brake = gd.VolumeBrake(_settings(), sleep=clock.sleep, clock=clock)
    return gd.GoogleDiscovery(page, brake, sleep=clock.sleep), clock  # type: ignore[arg-type]


async def test_search_two_pages_unique_domains() -> None:
    q = "sekolah bandung"
    page = FakePage(
        {
            gd.search_url(q, 0): (gd.search_url(q, 0), _fx("google_search.html")),
            gd.search_url(q, 1): (gd.search_url(q, 1), _fx("google_search.html")),
        }
    )
    collector, _ = _collector(page)
    results = await collector.search(q)
    assert len(page.opened) == 2
    assert len({r.domain for r in results}) == len(results) == 3


async def test_search_captcha_raises_without_retrying() -> None:
    q = "x"
    sorry = "https://www.google.com/sorry/index"
    page = FakePage({gd.search_url(q): (sorry, _fx("google_sorry.html"))})
    collector, _ = _collector(page)
    with pytest.raises(CaptchaRequired) as exc:
        await collector.search(q)
    assert exc.value.details and exc.value.details["block"] == "captcha"
    assert len(page.opened) == 1


async def test_maps_search_opens_limited_places() -> None:
    q = "rumah sakit bandung"
    feed = gd.parse_maps_feed(_fx("google_maps_feed.html"))
    pages = {gd.maps_search_url(q): (gd.maps_search_url(q), _fx("google_maps_feed.html"))}
    for p in feed:
        pages[p.maps_url] = (p.maps_url, _fx("google_maps_place.html"))
    page = FakePage(pages)
    collector, _ = _collector(page)
    result = await collector.maps_search(q, max_lookups=1)
    assert len(result.feed) == 2
    assert len(result.listings) == 1
    assert page.opened == [gd.maps_search_url(q), feed[0].maps_url]


async def test_maps_search_direct_place_redirect() -> None:
    q = "RS Borromeus"
    final = "https://www.google.com/maps/place/RS/data=!3d-6.8932!4d107.6129"
    page = FakePage({gd.maps_search_url(q): (final, _fx("google_maps_place.html"))})
    collector, _ = _collector(page)
    result = await collector.maps_search(q, max_lookups=5)
    assert [li.name for li in result.listings] == ["Rumah Sakit Santo Borromeus"]
    assert len(page.opened) == 1
