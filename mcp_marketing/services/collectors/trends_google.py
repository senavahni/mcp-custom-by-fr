"""Klien widget Google Trends (`/trends/api/explore` + `/trends/api/widgetdata/*`).

Satu implementasi, dua transport:
- `HttpxTransport`  -> adapter `unofficial` (HTTP langsung, cookie dari halaman utama),
- `BrowserTransport` -> adapter `browser` (XHR di dalam tab Chrome profil; cookie & sesi asli).
Nilai relatif 0–100 hanya sebanding dalam satu permintaan (maks 5 kata kunci).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
import orjson

from mcp_marketing.core.errors import UpstreamError, UpstreamTimeout, ValidationFailed
from mcp_marketing.services.browser.driver_pool import PageSource
from mcp_marketing.services.collectors.trends_base import (
    InterestResult,
    RegionValue,
    RelatedResult,
    TrendsRateLimited,
    parse_geo,
    parse_multiline,
    parse_related,
    strip_xssi,
)

TZ_OFFSET_MIN = -420  # Asia/Jakarta (Google memakai menit, tanda dibalik)
GPROP = {"web": "", "news": "news", "images": "images", "youtube": "youtube", "froogle": "froogle"}
MAX_KEYWORDS = 5
_SYNC_XHR = (
    "var x = new XMLHttpRequest(); x.open('GET', arguments[0], false); x.send(null);"
    "return [x.status, x.responseText];"
)


class Transport(Protocol):
    async def get_text(self, path: str, params: dict[str, Any]) -> str: ...


class HttpxTransport:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self._warmed = False

    async def _warm(self) -> None:
        if not self._warmed:
            try:
                await self.http.get(f"{self.base_url}/?geo=ID", timeout=20)
            except httpx.HTTPError:
                pass
            self._warmed = True

    async def get_text(self, path: str, params: dict[str, Any]) -> str:
        await self._warm()
        try:
            resp = await self.http.get(
                f"{self.base_url}{path}",
                params=params,
                timeout=30,
                headers={"Accept-Language": "id-ID,id;q=0.9"},
            )
        except httpx.TimeoutException:
            raise UpstreamTimeout("Google Trends timeout") from None
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Google Trends tidak dapat dihubungi ({type(exc).__name__})"
            ) from None
        if resp.status_code == 429:
            raise TrendsRateLimited("Google Trends 429 (terlalu banyak permintaan)", retryable=True)
        if resp.status_code >= 400:
            raise UpstreamError(
                f"Google Trends error {resp.status_code}", retryable=resp.status_code >= 500
            )
        return resp.text


class BrowserTransport:
    """XHR sinkron di tab trends.google.com milik profil Chrome (memakai cookie & sesi profil)."""

    def __init__(self, page: PageSource, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self._opened = False

    async def get_text(self, path: str, params: dict[str, Any]) -> str:
        if not self._opened:
            await self.page.open(f"{self.base_url}/trends/explore?geo=ID&hl=id")
            self._opened = True
        status, body = await self.page.run_js(_SYNC_XHR, f"{path}?{urlencode(params)}")
        if int(status) == 429:
            raise TrendsRateLimited("Google Trends 429 (browser)", retryable=True)
        if int(status) >= 400:
            raise UpstreamError(
                f"Google Trends error {status} (browser)", retryable=int(status) >= 500
            )
        return str(body)


def _time_range(start: date, end: date) -> str:
    return f"{start.isoformat()} {end.isoformat()}"


class GoogleTrendsClient:
    def __init__(
        self,
        transport: Transport,
        *,
        name: str,
        hl: str = "id",
        min_interval_s: float = 5.0,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.name = name
        self.hl = hl
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._calls = 0

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self._calls:
            await self._sleep(self.min_interval_s)  # jeda antar permintaan ≥ 5 s
        self._calls += 1
        return strip_xssi(
            await self.transport.get_text(path, {"hl": self.hl, "tz": TZ_OFFSET_MIN, **params})
        )

    async def _widgets(
        self, keywords: list[str], start: date, end: date, geo: str, category: int, gprop: str
    ) -> dict[str, dict[str, Any]]:
        if not 1 <= len(keywords) <= MAX_KEYWORDS:
            raise ValidationFailed(f"Google Trends: 1–{MAX_KEYWORDS} kata kunci per permintaan")
        req = {
            "comparisonItem": [
                {"keyword": k, "geo": geo, "time": _time_range(start, end)} for k in keywords
            ],
            "category": category,
            "property": GPROP.get(gprop, ""),
        }
        body = await self._get("/trends/api/explore", {"req": orjson.dumps(req).decode()})
        widgets: dict[str, dict[str, Any]] = {}
        for w in body.get("widgets") or []:
            widgets.setdefault(str(w.get("id")), w)
        return widgets

    async def _widget_data(self, endpoint: str, widget: dict[str, Any]) -> Any:
        return await self._get(
            f"/trends/api/widgetdata/{endpoint}",
            {"req": orjson.dumps(widget["request"]).decode(), "token": widget["token"]},
        )

    async def interest_over_time(
        self, keywords: list[str], start: date, end: date, geo: str, category: int, gprop: str
    ) -> InterestResult:
        widgets = await self._widgets(keywords, start, end, geo, category, gprop)
        widget = widgets.get("TIMESERIES")
        if widget is None:
            raise UpstreamError("Widget TIMESERIES tidak tersedia")
        result = parse_multiline(await self._widget_data("multiline", widget), keywords)
        result.provider = self.name
        return result

    async def interest_by_region(
        self, keyword: str, start: date, end: date, geo: str, resolution: str
    ) -> list[RegionValue]:
        widgets = await self._widgets([keyword], start, end, geo, 0, "web")
        widget = widgets.get("GEO_MAP")
        if widget is None:
            raise UpstreamError("Widget GEO_MAP tidak tersedia")
        widget = {**widget, "request": {**widget["request"], "resolution": resolution}}
        return parse_geo(await self._widget_data("comparedgeo", widget))

    async def related(
        self, keyword: str, start: date, end: date, geo: str, kind: str
    ) -> RelatedResult:
        widgets = await self._widgets([keyword], start, end, geo, 0, "web")
        widget = widgets.get("RELATED_QUERIES" if kind == "queries" else "RELATED_TOPICS")
        if widget is None:
            return RelatedResult()
        return parse_related(await self._widget_data("relatedsearches", widget), kind)


class OfficialTrendsProvider:
    """Google Trends API resmi (alpha, terbatas); aktif setelah `TRENDS_OFFICIAL_API_KEY` diisi."""

    name = "official"

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    def _unavailable(self) -> UpstreamError:
        return UpstreamError(
            "Google Trends API resmi belum diaktifkan (akses alpha belum disetujui)",
            retryable=False,
            details={"provider": "official"},
        )

    async def interest_over_time(self, *args: Any, **kwargs: Any) -> InterestResult:
        raise self._unavailable()

    async def interest_by_region(self, *args: Any, **kwargs: Any) -> list[RegionValue]:
        raise self._unavailable()

    async def related(self, *args: Any, **kwargs: Any) -> RelatedResult:
        raise self._unavailable()
