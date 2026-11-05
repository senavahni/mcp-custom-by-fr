"""TASK-14: guard SSRF, robots, rate limit domain, dan ekstraksi konten."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mcp_marketing.core.errors import Forbidden, ValidationFailed
from mcp_marketing.services.browser import extract as ex
from mcp_marketing.services.browser.politeness import DomainRateLimiter, RobotsCache
from mcp_marketing.services.browser.ssrf import check_url
from mcp_marketing.utils.tokens import count_tokens
from mcp_marketing.worker.handlers.web import normalize_url

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/admin",
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.10/",
        "http://metadata.google.internal/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
async def test_ssrf_blocks_private_and_metadata(url: str) -> None:
    with pytest.raises(Forbidden) as err:
        await check_url(url)
    assert err.value.details["reason"] == "ssrf_denied"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:1"])
async def test_ssrf_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(ValidationFailed):
        await check_url(url)


async def test_ssrf_allows_public_ip_and_private_when_enabled() -> None:
    await check_url("https://93.184.216.34/")
    await check_url("http://10.0.0.1/", allow_private=True)


def test_normalize_url_strips_tracking_and_fragment() -> None:
    url = "https://Example.com/a?utm_source=x&id=7&fbclid=abc#top"
    assert normalize_url(url) == "https://example.com/a?id=7"


async def test_robots_disallow_and_fetch_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "down.test":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        robots = RobotsCache(http)
        assert await robots.allowed("https://site.test/public/page")
        assert not await robots.allowed("https://site.test/private/x")
        assert await robots.allowed("https://down.test/anything")


async def test_domain_rate_limiter_spaces_requests_per_domain() -> None:
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)

    limiter = DomainRateLimiter(30, jitter=(0.0, 0.0), sleep=fake_sleep, clock=lambda: 100.0)
    await limiter.wait("https://a.test/1")
    await limiter.wait("https://a.test/2")
    await limiter.wait("https://b.test/1")
    assert waits == [2.0]  # 30 rpm -> 2 s untuk permintaan kedua di domain sama


def test_extract_article_golden() -> None:
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    out = ex.extract(html, "https://blog.test/artikel/kopi", mode="markdown")
    assert out.title == "Tren Kopi Susu 2026 - Blog Contoh"
    assert out.published_at is not None and out.published_at.isoformat().startswith("2026-09-15")
    assert "Kopi susu gula aren" in out.content
    assert "Peluang untuk pemilik kedai" in out.content
    for noise in ("dataLayer", "cookie", "Kebijakan Privasi", "tracking"):
        assert noise not in out.content
    assert len(out.content_hash) == 64


def test_extract_reduces_tokens_by_70_percent() -> None:
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    out = ex.extract(html, "https://blog.test/artikel/kopi")
    assert count_tokens(out.content) <= 0.3 * count_tokens(html)


def test_extract_selectors_and_links_modes() -> None:
    html = (FIXTURES / "article.html").read_text(encoding="utf-8")
    sel = ex.extract(
        html,
        "https://blog.test/",
        mode="selectors",
        selectors={"h1": "h1", "popular": "aside li a::attr(href)"},
    )
    assert sel.extracted == {
        "h1": "Tren Kopi Susu 2026",
        "popular": ["/artikel/1", "/artikel/2", "/artikel/3", "/artikel/4"],
    }
    found = ex.links(html, "https://blog.test/", same_domain=True)
    urls = [link["url"] for link in found]
    assert "https://blog.test/artikel/1" in urls
    assert not any("twitter.com" in u for u in urls)


def test_detect_block_captcha_and_status() -> None:
    assert ex.detect_block("<div class='g-recaptcha'></div>", "Halaman")
    assert ex.detect_block("<p>ok</p>", "Just a moment...")
    assert ex.detect_block("<p>ok</p>", "Halaman", http_status=429)
    assert not ex.detect_block("<p>artikel biasa</p>", "Artikel")


def test_recaptcha_on_content_page_is_not_a_block() -> None:
    # situs perusahaan sering memasang reCAPTCHA di form kontak: konten tetap harus diambil
    body = "<p>" + "PT Contoh adalah perusahaan percetakan sekuriti. " * 40 + "</p>"
    html = (
        f"<html><body>{body}<form><div class='g-recaptcha'></div></form>"
        "<script src='https://www.google.com/recaptcha/api.js'></script></body></html>"
    )
    assert not ex.detect_block(html, "Leading Security Printing Company")
    # halaman tantangan: hampir tanpa teks + widget captcha → tetap diblokir
    challenge = "<html><body><p>Verifikasi</p><div class='g-recaptcha'></div></body></html>"
    assert ex.detect_block(challenge, "Cek")
