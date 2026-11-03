"""Ekstraksi halaman web hemat token: konten utama -> markdown, selectors, links, tanggal terbit.

`trafilatura` membuang nav/footer/iklan; `markdownify` sebagai cadangan. Hasil dinormalisasi dan
dipotong `max_chars` (MCP_SPEC §3 T10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import orjson
import trafilatura
from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from mcp_marketing.core.errors import ValidationFailed
from mcp_marketing.utils.crypto import sha256_hex
from mcp_marketing.utils.text import normalize_ws, truncate

Mode = Literal["text", "markdown", "selectors", "links"]
MAX_SELECTORS = 50
MAX_LINKS = 200
LINK_TEXT_MAX = 80
MAX_STORED_CHARS = 200_000
_BLOCK_TITLES = re.compile(
    r"captcha|access denied|attention required|just a moment|are you a robot|robot check", re.I
)
_PUBLISHED_META = (
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[name="pubdate"]',
    'meta[name="publishdate"]',
    'meta[itemprop="datePublished"]',
    'meta[name="date"]',
)


@dataclass
class Extracted:
    title: str | None
    content: str
    chars: int
    content_hash: str
    published_at: datetime | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    extracted: dict[str, Any] | None = None


def detect_block(html: str, title: str | None, http_status: int | None = None) -> bool:
    """Halaman blokir/CAPTCHA tidak dicoba dilewati (status `blocked`)."""
    if http_status in (403, 429):
        return True
    if title and _BLOCK_TITLES.search(title):
        return True
    lowered = html[:200_000].lower()
    return (
        'iframe[src*="recaptcha"]' in lowered
        or "g-recaptcha" in lowered
        or "hcaptcha.com" in lowered
        or ("/recaptcha/" in lowered and "<iframe" in lowered)
    )


def _title(tree: HTMLParser) -> str | None:
    node = tree.css_first("title")
    text = node.text(strip=True) if node else ""
    return truncate(text, 500) if text else None


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def published_at(tree: HTMLParser) -> datetime | None:
    for sel in _PUBLISHED_META:
        node = tree.css_first(sel)
        if node and (dt := _parse_dt(str(node.attributes.get("content") or ""))):
            return dt
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = orjson.loads(script.text() or "null")
        except orjson.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                raw_graph = item.get("@graph")
                graph: list[Any] = raw_graph if isinstance(raw_graph, list) else [item]
                for entry in graph:
                    if isinstance(entry, dict) and isinstance(entry.get("datePublished"), str):
                        dt = _parse_dt(entry["datePublished"])
                        if dt:
                            return dt
    time_node = tree.css_first("time[datetime]")
    return _parse_dt(str(time_node.attributes.get("datetime") or "")) if time_node else None


def main_markdown(html: str, url: str) -> str:
    text = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_precision=True,
    )
    if not text:
        tree = HTMLParser(html)
        for sel in (
            "script",
            "style",
            "noscript",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
            "iframe",
        ):
            for node in tree.css(sel):
                node.decompose()
        body = tree.body.html if tree.body else html
        text = markdownify(body or "", heading_style="ATX", strip=["img"])
    return normalize_ws(text)


def links(html: str, base_url: str, *, same_domain: bool = False) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    base_host = (urlsplit(base_url).hostname or "").lower()
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in tree.css("a[href]"):
        href = str(a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or absolute in seen:
            continue
        if same_domain and (parts.hostname or "").lower() != base_host:
            continue
        seen.add(absolute)
        out.append({"url": absolute, "text": truncate(normalize_ws(a.text() or ""), LINK_TEXT_MAX)})
        if len(out) >= MAX_LINKS:
            break
    return out


def run_selectors(html: str, selectors: dict[str, str]) -> dict[str, Any]:
    """`{name: css}` atau `{name: "css::attr(href)"}` -> nilai tunggal / list (maks 50)."""
    if len(selectors) > MAX_SELECTORS:
        raise ValidationFailed(f"selectors maksimal {MAX_SELECTORS}")
    tree = HTMLParser(html)
    result: dict[str, Any] = {}
    for name, spec in selectors.items():
        css, attr = spec, None
        if "::attr(" in spec and spec.endswith(")"):
            css, attr = spec.split("::attr(", 1)
            attr = attr[:-1]
        try:
            nodes = tree.css(css.strip())
        except ValueError:
            raise ValidationFailed(f"CSS selector tidak valid untuk '{name}'") from None
        values = [
            (str(n.attributes.get(attr) or "") if attr else normalize_ws(n.text() or ""))
            for n in nodes[:MAX_SELECTORS]
        ]
        values = [truncate(v, 1_000) for v in values if v]
        result[name] = values[0] if len(values) == 1 else values
    return result


def extract(
    html: str,
    url: str,
    *,
    mode: Mode = "markdown",
    selectors: dict[str, str] | None = None,
    max_chars: int = 20_000,
) -> Extracted:
    tree = HTMLParser(html)
    title = _title(tree)
    pub = published_at(tree)
    if mode == "selectors":
        data = run_selectors(html, selectors or {})
        content = truncate(orjson.dumps(data).decode(), max_chars)
        return Extracted(title, content, len(content), sha256_hex(content), pub, extracted=data)
    if mode == "links":
        found = links(html, url)
        content = "\n".join(f"- [{link['text'] or link['url']}]({link['url']})" for link in found)
        return Extracted(
            title, truncate(content, max_chars), len(content), sha256_hex(content), pub, links=found
        )
    content = main_markdown(html, url)
    if mode == "text":
        content = re.sub(r"[#*_`>|]+", "", content)
        content = normalize_ws(content)
    full_hash = sha256_hex(content)
    return Extracted(
        title,
        truncate(content, max_chars),
        len(content),
        full_hash,
        pub,
        links=links(html, url, same_domain=True),
    )


def stored_markdown(content: str) -> str:
    return content[:MAX_STORED_CHARS]
