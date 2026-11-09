"""Discovery perusahaan via Chrome: Google Search & Google Maps volume rendah (MCP-V1-06, V1-K4).

- Parser murni (HTML -> data) memakai selector `google_selectors` saja; dites dengan fixture HTML.
- `detect_block`: halaman `sorry`/reCAPTCHA/consent -> `CaptchaRequired`. TIDAK ada pemecah
  CAPTCHA, rotasi proxy, atau penyamaran sidik jari: job pause, user menyelesaikan di Chrome.
- `VolumeBrake`: batas per jam & per hari per jenis + jeda acak antar navigasi (in-memory per
  proses/profil; persistensi kuota lintas job menyusul bersama `ci_discovery_status`).
- Maps: hanya data listing publik (nama, kategori, alamat, telepon, website, koordinat, rating
  ringkas). Ulasan & data reviewer tidak diambil.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from mcp_marketing.core.config import DiscoverySettings
from mcp_marketing.core.errors import CaptchaRequired, RateLimited
from mcp_marketing.services.browser.driver_pool import PageSource
from mcp_marketing.services.collectors import google_selectors as sel

Kind = Literal["google_search", "google_maps"]
BlockKind = Literal["captcha", "consent"]

SEARCH_PAGE_SIZE = 10
SEARCH_MAX_PAGES = 2
MAPS_MAX_FEED_SCROLLS = 6
MAPS_SCROLL_PAUSE_S = (1.5, 3.5)
FEED_SCROLL_JS = (
    "const f=document.querySelector(arguments[0]); if(f){f.scrollBy(0, f.scrollHeight);}"
)
_COORD_BANG_RE = re.compile(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)")
_COORD_AT_RE = re.compile(r"@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)")
_RATING_RE = re.compile(r"(\d)[.,](\d)")
_DIGITS_RE = re.compile(r"\d[\d.,]*")

# Bukan situs perusahaan: marketplace, direktori, sosial, ensiklopedia, portal Google.
DEFAULT_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        # marketplace
        "tokopedia.com", "shopee.co.id", "bukalapak.com", "lazada.co.id", "blibli.com",
        "bhinneka.com", "olx.co.id", "alibaba.com", "indotrading.com", "ralali.com",
        # direktori & agregator
        "indonetwork.co.id", "yellowpages.co.id", "kompass.com", "dnb.com", "zoominfo.com",
        "glints.com", "jobstreet.co.id", "kalibrr.com", "loker.id",
        "referensi.data.kemdikbud.go.id", "sekolah.data.kemdikbud.go.id",
        "tripadvisor.com", "tripadvisor.co.id", "foursquare.com", "waze.com", "trustpilot.com",
        # sosial & video
        "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
        "youtube.com", "threads.net", "pinterest.com",
        # ensiklopedia, berita agregator, Google
        "wikipedia.org", "wikimapia.org", "google.com", "google.co.id", "goo.gl", "g.page",
    }
)  # fmt: skip


# ---------------------------------------------------------------------------- data


@dataclass(frozen=True)
class SearchResult:
    rank: int
    url: str
    domain: str
    title: str
    snippet: str | None


@dataclass(frozen=True)
class MapsListing:
    name: str
    maps_url: str
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None


@dataclass(frozen=True)
class FeedPlace:
    name: str
    maps_url: str
    lat: float | None
    lng: float | None


# ---------------------------------------------------------------------------- util


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_blocked(domain: str, blocked: Iterable[str]) -> bool:
    """Cocok bila domain sama atau subdomain dari entri daftar blokir."""
    return any(domain == b or domain.endswith("." + b) for b in blocked)


def blocked_domains(settings: DiscoverySettings) -> frozenset[str]:
    extra = {d.strip().lower() for d in settings.discovery_blocked_domains.split(",") if d.strip()}
    return DEFAULT_BLOCKED_DOMAINS | extra


def unwrap_google_redirect(href: str) -> str:
    """`/url?q=https://x&sa=...` atau `https://www.google.com/url?q=...` -> URL tujuan."""
    parts = urlsplit(href)
    if parts.path == "/url" and (not parts.netloc or "google." in parts.netloc):
        qs = parse_qs(parts.query)
        target = (qs.get("q") or qs.get("url") or [""])[0]
        return target or href
    return href


def coords_from_url(url: str) -> tuple[float | None, float | None]:
    """Koordinat tempat dari URL Maps: `!3d<lat>!4d<lng>` (tempat) lebih akurat dari `@lat,lng`."""
    for pattern in (_COORD_BANG_RE, _COORD_AT_RE):
        m = pattern.search(url)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng
    return None, None


def _strip_label(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for prefix in sel.LABEL_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text or None


def detect_block(url: str, html: str) -> BlockKind | None:
    if any(m in url for m in sel.CONSENT_URL_MARKERS):
        return "consent"
    if any(m in url for m in sel.CAPTCHA_URL_MARKERS):
        return "captcha"
    lowered = html[:200_000].lower()
    if any(m.lower() in lowered for m in sel.CAPTCHA_HTML_MARKERS):
        return "captcha"
    return None


# ---------------------------------------------------------------------------- parser


def search_url(query: str, page: int = 0) -> str:
    start = f"&start={page * SEARCH_PAGE_SIZE}" if page else ""
    return f"https://www.google.com/search?q={quote_plus(query)}&hl=id&gl=id{start}"


def parse_search_html(
    html: str, blocked: Iterable[str] = DEFAULT_BLOCKED_DOMAINS, *, rank_offset: int = 0
) -> list[SearchResult]:
    """Hasil organik unik per domain; iklan & domain terblokir dibuang."""
    tree = HTMLParser(html)
    for ad in tree.css(sel.SEARCH_ADS):
        ad.decompose()
    root = tree.css_first(sel.SEARCH_RESULTS_ROOT)
    if root is None:
        return []
    blocked_set = frozenset(blocked)
    out: list[SearchResult] = []
    seen: set[str] = set()
    for link in root.css(sel.SEARCH_RESULT_LINK):
        title_node = link.css_first(sel.SEARCH_RESULT_TITLE)
        if title_node is None:
            continue
        url = unwrap_google_redirect(str(link.attributes.get("href") or ""))
        if not url.startswith(("http://", "https://")):
            continue
        domain = host_of(url)
        if not domain or domain in seen or is_blocked(domain, blocked_set):
            continue
        seen.add(domain)
        out.append(
            SearchResult(
                rank=rank_offset + len(out) + 1,
                url=url,
                domain=domain,
                title=title_node.text(strip=True),
                snippet=_snippet_near(link),
            )
        )
    return out


def _snippet_near(link: object) -> str | None:
    """Snippet = elemen snippet pertama di kontainer hasil (naik maks 6 tingkat dari tautan)."""
    node = link
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None:
            return None
        found = node.css_first(sel.SEARCH_RESULT_SNIPPET)  # type: ignore[attr-defined]
        if found is not None:
            text = found.text(separator=" ", strip=True)
            return text[:500] or None
    return None


def maps_search_url(query: str) -> str:
    return f"https://www.google.com/maps/search/{quote(query)}?hl=id"


def parse_maps_feed(html: str) -> list[FeedPlace]:
    tree = HTMLParser(html)
    feed = tree.css_first(sel.MAPS_FEED)
    if feed is None:
        return []
    out: list[FeedPlace] = []
    seen: set[str] = set()
    for link in feed.css(sel.MAPS_PLACE_LINK):
        href = str(link.attributes.get("href") or "")
        name = str(link.attributes.get("aria-label") or "").strip()
        key = href.split("?")[0]
        if not href or not name or key in seen:
            continue
        seen.add(key)
        lat, lng = coords_from_url(href)
        out.append(FeedPlace(name=name, maps_url=href, lat=lat, lng=lng))
    return out


def parse_maps_place(html: str, url: str) -> MapsListing | None:
    tree = HTMLParser(html)
    name_node = tree.css_first(sel.MAPS_NAME)
    name = name_node.text(strip=True) if name_node else ""
    if not name:
        return None

    def text_of(css: str, attr: str | None = None) -> str | None:
        node = tree.css_first(css)
        if node is None:
            return None
        raw = node.attributes.get(attr) if attr else node.text(strip=True)
        return _strip_label(str(raw)) if raw else None

    phone_node = tree.css_first(sel.MAPS_PHONE)
    phone = None
    if phone_node is not None:
        phone = str(phone_node.attributes.get("data-item-id") or "").removeprefix("phone:tel:")
    website_node = tree.css_first(sel.MAPS_WEBSITE)
    website = (
        unwrap_google_redirect(str(website_node.attributes.get("href") or ""))
        if website_node is not None
        else None
    )
    rating_raw = text_of(sel.MAPS_RATING)
    rating_m = _RATING_RE.search(rating_raw or "")
    reviews_raw = text_of(sel.MAPS_REVIEW_COUNT, "aria-label")
    reviews_m = _DIGITS_RE.search(reviews_raw or "")
    lat, lng = coords_from_url(url)
    return MapsListing(
        name=name,
        maps_url=url,
        category=text_of(sel.MAPS_CATEGORY),
        address=text_of(sel.MAPS_ADDRESS, "aria-label") or text_of(sel.MAPS_ADDRESS),
        phone=phone or None,
        website=website or None,
        lat=lat,
        lng=lng,
        rating=float(f"{rating_m.group(1)}.{rating_m.group(2)}") if rating_m else None,
        review_count=int(re.sub(r"[.,]", "", reviews_m.group(0))) if reviews_m else None,
    )


# ---------------------------------------------------------------------------- rem volume


@dataclass
class _Window:
    per_hour: int
    per_day: int
    stamps: deque[float] = field(default_factory=deque)
    day: date | None = None
    day_count: int = 0


class VolumeBrake:
    """Rem volume per profil: maks N/jam & N/hari per jenis + jeda acak antar navigasi Google."""

    def __init__(
        self,
        settings: DiscoverySettings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._today = today
        self._windows: dict[Kind, _Window] = {
            "google_search": _Window(
                settings.discovery_search_per_hour, settings.discovery_search_per_day
            ),
            "google_maps": _Window(
                settings.discovery_maps_per_hour, settings.discovery_maps_per_day
            ),
        }
        self._last_nav: float | None = None

    def remaining(self, kind: Kind) -> dict[str, int]:
        w = self._windows[kind]
        self._roll(w)
        return {"hour": w.per_hour - len(w.stamps), "day": w.per_day - w.day_count}

    def _roll(self, w: _Window) -> None:
        now, today = self._clock(), self._today()
        while w.stamps and now - w.stamps[0] >= 3_600:
            w.stamps.popleft()
        if w.day != today:
            w.day, w.day_count = today, 0

    async def acquire(self, kind: Kind) -> float:
        """Tunggu sampai navigasi boleh; kembalikan detik menunggu. Kuota harian habis -> error."""
        w = self._windows[kind]
        self._roll(w)
        if w.day_count >= w.per_day:
            raise RateLimited(
                f"Kuota harian {kind} habis ({w.per_day}); lanjut besok",
                retryable=True,
                details={"kind": kind, "per_day": w.per_day},
            )
        waited = 0.0
        if len(w.stamps) >= w.per_hour:
            delay = 3_600 - (self._clock() - w.stamps[0])
            if delay > 0:
                await self._sleep(delay)
                waited += delay
            self._roll(w)
        if self._last_nav is not None:
            gap = random.uniform(
                self.settings.discovery_pause_min_s, self.settings.discovery_pause_max_s
            )
            delay = gap - (self._clock() - self._last_nav)
            if delay > 0:
                await self._sleep(delay)
                waited += delay
        now = self._clock()
        w.stamps.append(now)
        w.day_count += 1
        self._last_nav = now
        return waited


# ---------------------------------------------------------------------------- kolektor


@dataclass
class MapsSearchResult:
    query: str
    feed: list[FeedPlace]
    listings: list[MapsListing]


class GoogleDiscovery:
    """Navigasi Chrome untuk Search & Maps. Setiap navigasi melewati `VolumeBrake`."""

    def __init__(
        self,
        page: PageSource,
        brake: VolumeBrake,
        *,
        blocked: Iterable[str] = DEFAULT_BLOCKED_DOMAINS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.page = page
        self.brake = brake
        self.blocked = frozenset(blocked)
        self._sleep = sleep

    async def _open(self, kind: Kind, url: str, *, wait_for: str | None = None) -> tuple[str, str]:
        await self.brake.acquire(kind)
        fetched = await self.page.open(url, wait_for=wait_for)
        block = detect_block(fetched.final_url, fetched.html)
        if block is not None:
            raise CaptchaRequired(
                "Google meminta verifikasi; selesaikan di jendela Chrome lalu lanjutkan job",
                details={"kind": kind, "block": block, "url": fetched.final_url[:300]},
            )
        return fetched.final_url, fetched.html

    async def search(self, query: str, *, pages: int = SEARCH_MAX_PAGES) -> list[SearchResult]:
        """Hasil organik halaman 1..`pages` (maks 2), unik per domain."""
        results: list[SearchResult] = []
        seen: set[str] = set()
        for page_no in range(min(pages, SEARCH_MAX_PAGES)):
            _, html = await self._open("google_search", search_url(query, page_no))
            batch = parse_search_html(html, self.blocked, rank_offset=len(results))
            fresh = [r for r in batch if r.domain not in seen]
            seen.update(r.domain for r in fresh)
            results.extend(fresh)
            if not batch:
                break
        return results

    async def maps_search(self, query: str, *, max_lookups: int) -> MapsSearchResult:
        """Buka daftar Maps untuk query; buka maks `max_lookups` halaman tempat (rem per tempat)."""
        final_url, html = await self._open("google_maps", maps_search_url(query))
        if "/maps/place/" in final_url:  # satu kecocokan: Maps langsung membuka tempat
            listing = parse_maps_place(html, final_url)
            feed = [FeedPlace(listing.name, final_url, listing.lat, listing.lng)] if listing else []
            return MapsSearchResult(query, feed, [listing] if listing else [])
        feed = await self._scroll_feed(html, want=max_lookups)
        listings: list[MapsListing] = []
        for place in feed[:max_lookups]:
            url, place_html = await self._open(
                "google_maps", place.maps_url, wait_for=sel.MAPS_NAME
            )
            listing = parse_maps_place(place_html, url)
            if listing is not None:
                listings.append(listing)
        return MapsSearchResult(query, feed, listings)

    async def _scroll_feed(self, html: str, *, want: int) -> list[FeedPlace]:
        feed = parse_maps_feed(html)
        for _ in range(MAPS_MAX_FEED_SCROLLS):
            if len(feed) >= want:
                break
            await self.page.run_js(FEED_SCROLL_JS, sel.MAPS_FEED)
            await self._sleep(random.uniform(*MAPS_SCROLL_PAUSE_S))
            more = parse_maps_feed(await self.page.html())
            if len(more) <= len(feed):
                break
            feed = more
        return feed
