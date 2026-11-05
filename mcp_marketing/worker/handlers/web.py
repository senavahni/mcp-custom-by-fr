"""Job web: `web_scrape`, `web_crawl`, `web_extract`, `browser_profile_check` (TASK-14).

Semua akses browser lewat `launcher_for(services)` (satu driver per job, profil terkunci).
Konten disimpan di `mcp.web_pages`; LLM hanya menerima markdown terpotong atau handle.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import create_model
from sqlalchemy import select

from mcp_marketing.core.errors import Forbidden, ToolError, UpstreamTimeout, ValidationFailed
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.browser import WebPage
from mcp_marketing.models.results import ResultItem
from mcp_marketing.schemas.common import JobEstimate
from mcp_marketing.services.browser import extract as ex
from mcp_marketing.services.browser.driver_pool import PageSource, launcher_for, profile_store
from mcp_marketing.services.browser.login_window import LOGIN_URLS, opener_for
from mcp_marketing.services.browser.politeness import DomainRateLimiter, RobotsCache, domain_of
from mcp_marketing.services.browser.ssrf import check_url
from mcp_marketing.services.results.result_store import Owner
from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.time import now
from mcp_marketing.utils.tokens import truncate_to_tokens
from mcp_marketing.worker.handlers.base import JobCtx, JobResult

log = get_logger(__name__)

PAGE_COLS = ["url", "title", "status", "chars", "content_hash", "published_at"]
INLINE_MAX_PAGES = 3
INLINE_TOKENS = 1_200
EXTRACT_PAGE_CHARS = 6_000
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|igshid)$", re.I)


@dataclass
class ScrapeOptions:
    mode: ex.Mode = "markdown"
    selectors: dict[str, str] | None = None
    wait_for: str | None = None
    scroll: int = 0
    max_chars: int = 20_000
    respect_robots: bool = True


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k)
        ]
    )
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), (parts.netloc or "").lower(), path, query, ""))


class PageScraper:
    """Satu halaman: SSRF -> robots -> rate limit -> buka -> cek blokir -> ekstrak -> simpan."""

    def __init__(self, job: JobCtx, opts: ScrapeOptions) -> None:
        self.job = job
        self.opts = opts
        s = job.services.settings.browser
        self.allow_private = s.scrape_allow_private_network
        self.robots = RobotsCache(job.services.http)
        self.limiter: DomainRateLimiter = job.services.extras.get(
            "domain_limiter"
        ) or DomainRateLimiter(s.scrape_per_domain_rpm)

    async def scrape(
        self, page: PageSource, url: str
    ) -> tuple[dict[str, Any], ex.Extracted | None, list[dict[str, str]]]:
        record: dict[str, Any] = {
            "url": url,
            "final_url": None,
            "status": "ok",
            "http_status": None,
            "title": None,
        }
        extracted: ex.Extracted | None = None
        found_links: list[dict[str, str]] = []
        try:
            await check_url(url, allow_private=self.allow_private)
            if self.opts.respect_robots and not await self.robots.allowed(url):
                record["status"] = "skipped_robots"
            else:
                await self.limiter.wait(url)
                fetched = await page.open(url, wait_for=self.opts.wait_for)
                html = fetched.html
                if self.opts.scroll:
                    await page.scroll(self.opts.scroll)
                    html = await page.html()
                record.update(
                    final_url=fetched.final_url, http_status=fetched.status, title=fetched.title
                )
                await check_url(fetched.final_url, allow_private=self.allow_private)
                if ex.detect_block(html, fetched.title, fetched.status):
                    record["status"] = "blocked"
                elif fetched.status and fetched.status >= 400:
                    record["status"] = "http_error"
                else:
                    extracted = ex.extract(
                        html,
                        fetched.final_url,
                        mode=self.opts.mode,
                        selectors=self.opts.selectors,
                        max_chars=self.opts.max_chars,
                    )
                    found_links = ex.links(html, fetched.final_url)
                    record["title"] = extracted.title or fetched.title
        except Forbidden:
            record["status"] = "ssrf_denied"
        except UpstreamTimeout:
            record["status"] = "timeout"
        except ValidationFailed as err:
            record.update(status="invalid", error=err.message)
        page_id = await self._save(record, extracted)
        record.update(
            page_id=page_id,
            chars=extracted.chars if extracted else 0,
            content_hash=extracted.content_hash if extracted else None,
            published_at=extracted.published_at.isoformat()
            if extracted and extracted.published_at
            else None,
        )
        return record, extracted, found_links

    async def _save(self, record: dict[str, Any], extracted: ex.Extracted | None) -> str:
        page_id = uuid.uuid4()
        async with self.job.services.db.session() as session:
            session.add(
                WebPage(
                    id=page_id,
                    job_id=self.job.job_id,
                    url=record["url"][:4000],
                    final_url=record.get("final_url"),
                    domain=domain_of(record.get("final_url") or record["url"])[:255] or "-",
                    status=record["status"],
                    http_status=record.get("http_status"),
                    title=record.get("title"),
                    published_at=extracted.published_at if extracted else None,
                    content_md=ex.stored_markdown(extracted.content) if extracted else None,
                    extracted=extracted.extracted if extracted else None,
                    content_hash=extracted.content_hash if extracted else None,
                    chars=extracted.chars if extracted else 0,
                )
            )
        return str(page_id)


def _options(params: dict[str, Any]) -> ScrapeOptions:
    return ScrapeOptions(
        mode=params.get("mode", "markdown"),
        selectors=params.get("selectors"),
        wait_for=params.get("wait_for"),
        scroll=int(params.get("scroll") or 0),
        max_chars=int(params.get("max_chars_per_page") or 20_000),
        respect_robots=bool(params.get("respect_robots", True)),
    )


async def _pages_result(
    job: JobCtx,
    rows: list[dict[str, Any]],
    *,
    kind: str,
    summary: str,
    stats: dict[str, Any],
    inline_content: bool,
) -> JobResult:
    owner = Owner(client_id=job.client_id, run_id=job.run_id)
    rs = await job.services.results.create(
        kind="web_pages",
        columns=PAGE_COLS,
        rows=rows,
        owner=owner,
        job_id=job.job_id,
        ref_id="page_id",
        stats=stats,
        source_ref={"table": "web_pages", "job_id": job.job_id, "kind": kind},
    )
    result: dict[str, Any] = {
        "summary": summary,
        "handle": rs.handle,
        "total": rs.total,
        "stats": stats,
        "cost": {"llm_tokens": 0, "external_units": 0, "cache_hit": False},
    }
    if inline_content and rows:
        ids = [uuid.UUID(r["page_id"]) for r in rows[:INLINE_MAX_PAGES]]
        async with job.services.db.session() as session:
            pages = {
                str(p.id): p
                for p in (await session.scalars(select(WebPage).where(WebPage.id.in_(ids)))).all()
            }
        per_page = INLINE_TOKENS // max(1, len(ids))
        result["pages"] = [
            {
                "url": r["url"],
                "title": r.get("title"),
                "status": r["status"],
                "chars": r.get("chars", 0),
                "content": truncate_to_tokens(pages[r["page_id"]].content_md or "", per_page)
                if r["page_id"] in pages
                else "",
            }
            for r in rows[:INLINE_MAX_PAGES]
        ]
    else:
        result["items"] = {
            "cols": ["url", "title", "status", "chars"],
            "rows": [[r["url"], r.get("title"), r["status"], r.get("chars", 0)] for r in rows[:5]],
        }
    baseline = sum(int(r.get("chars", 0)) for r in rows) // 4
    result["stats"] = {**stats, "baseline_tokens": baseline}
    return JobResult(result=result, result_handle=rs.handle)


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


class WebScrapeHandler:
    kind = "web_scrape"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=6 * len(params.get("urls") or []) + 5)

    async def run(self, job: JobCtx) -> JobResult:
        urls: list[str] = list(job.params["urls"])
        profile = job.params.get("profile") or "default"
        scraper = PageScraper(job, _options(job.params))
        rows: list[dict[str, Any]] = list(job.checkpoint.get("rows", []))
        async with launcher_for(job.services).session(profile, job.job_id) as page:
            for i in range(int(job.checkpoint.get("next", 0)), len(urls)):
                await job.raise_if_cancelled()
                record, _, _ = await scraper.scrape(page, urls[i])
                rows.append({k: record.get(k) for k in (*PAGE_COLS, "page_id")})
                await job.save_checkpoint({"next": i + 1, "rows": rows})
                await job.progress(i + 1, len(urls), f"{i + 1}/{len(urls)} halaman")
        counts = _status_counts(rows)
        summary = f"{counts.get('ok', 0)}/{len(rows)} halaman berhasil diambil (profil {profile})"
        return await _pages_result(
            job,
            rows,
            kind=self.kind,
            summary=summary,
            stats={"status": counts, "profile": profile},
            inline_content=len(urls) <= INLINE_MAX_PAGES,
        )


class WebCrawlHandler:
    kind = "web_crawl"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=5 * int(params.get("max_pages") or 50))

    async def run(self, job: JobCtx) -> JobResult:
        p = job.params
        start = normalize_url(p["start_url"])
        max_pages, max_depth = int(p.get("max_pages") or 50), int(p.get("max_depth") or 2)
        same_domain = bool(p.get("same_domain", True))
        include = [re.compile(x) for x in p.get("include_patterns") or []]
        exclude = [re.compile(x) for x in p.get("exclude_patterns") or []]
        since = _dt(p.get("published_since"))
        until = _dt(p.get("published_until"))
        host = urlsplit(start).hostname or ""
        ck = job.checkpoint
        queue: list[list[Any]] = ck.get("queue", [[start, 0]])
        seen: set[str] = set(ck.get("seen", [start]))
        hashes: set[str] = set(ck.get("hashes", []))
        rows: list[dict[str, Any]] = list(ck.get("rows", []))
        visited = int(ck.get("visited", 0))
        scraper = PageScraper(job, _options(p))
        async with launcher_for(job.services).session(
            p.get("profile") or "default", job.job_id
        ) as page:
            while queue and visited < max_pages:
                await job.raise_if_cancelled()
                url, depth = queue.pop(0)
                record, extracted, found = await scraper.scrape(page, url)
                visited += 1
                keep = (
                    record["status"] != "ok"
                    or extracted is None
                    or extracted.content_hash not in hashes
                )
                pub = extracted.published_at if extracted else None
                if pub and ((since and pub < since) or (until and pub > until)):
                    keep = False
                if extracted is not None:
                    hashes.add(extracted.content_hash)
                if keep:
                    rows.append({k: record.get(k) for k in (*PAGE_COLS, "page_id")})
                if depth < max_depth:
                    for link in found:
                        nxt = normalize_url(link["url"])
                        if nxt in seen or (same_domain and (urlsplit(nxt).hostname or "") != host):
                            continue
                        if (include and not any(r.search(nxt) for r in include)) or any(
                            r.search(nxt) for r in exclude
                        ):
                            continue
                        seen.add(nxt)
                        queue.append([nxt, depth + 1])
                await job.save_checkpoint(
                    {
                        "queue": queue,
                        "seen": sorted(seen),
                        "hashes": sorted(hashes),
                        "rows": rows,
                        "visited": visited,
                    }
                )
                await job.progress(
                    visited, max_pages, f"{visited} halaman dikunjungi, {len(queue)} antre"
                )
        dates = sorted(r["published_at"] for r in rows if r.get("published_at"))
        stats = {
            "visited": visited,
            "kept": len(rows),
            "status": _status_counts(rows),
            "domains": sorted({domain_of(r["url"]) for r in rows})[:10],
            "published_range": [dates[0], dates[-1]] if dates else None,
        }
        summary = f"Crawl {host}: {visited} halaman dikunjungi, {len(rows)} disimpan"
        return await _pages_result(
            job, rows, kind=self.kind, summary=summary, stats=stats, inline_content=False
        )


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WebExtractHandler:
    kind = "web_extract"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=4 * int(params.get("max_pages") or 5) + 10)

    async def _pages(self, job: JobCtx) -> list[WebPage]:
        source = job.params["source"]
        max_pages = int(job.params.get("max_pages") or 5)
        if source.get("handle"):
            owner = Owner(client_id=job.client_id, run_id=job.run_id)
            async with job.services.db.session() as session:
                rs = await job.services.results.get_set(session, source["handle"], owner)
                if rs.kind != "web_pages":
                    raise ValidationFailed("handle bukan hasil web_scrape/web_crawl")
                ids = (
                    await session.scalars(
                        select(ResultItem.ref_id)
                        .where(ResultItem.handle == rs.handle)
                        .order_by(ResultItem.idx)
                        .limit(max_pages)
                    )
                ).all()
                pages = (
                    await session.scalars(
                        select(WebPage).where(WebPage.id.in_([i for i in ids if i]))
                    )
                ).all()
            order = {pid: i for i, pid in enumerate(ids)}
            return sorted((p for p in pages if p.status == "ok"), key=lambda p: order.get(p.id, 0))
        scraper = PageScraper(job, ScrapeOptions())
        async with launcher_for(job.services).session(
            job.params.get("profile") or "default", job.job_id
        ) as page:
            record, _, _ = await scraper.scrape(page, source["url"])
        async with job.services.db.session() as session:
            row = await session.get(WebPage, uuid.UUID(record["page_id"]))
        if row is None or row.status != "ok":
            raise ToolError(
                f"Halaman tidak bisa diambil ({record['status']})", code="UPSTREAM_ERROR"
            )
        return [row]

    async def run(self, job: JobCtx) -> JobResult:
        fields: dict[str, str] = job.params["schema"]
        instructions = job.params.get("instructions") or ""
        llm = job.services.llm
        if llm is None:
            raise ToolError("LLM Gateway tidak dikonfigurasi di worker", code="UPSTREAM_ERROR")
        model = create_model("Extracted", **{name: (Any, None) for name in fields})  # type: ignore[call-overload]
        pages = await self._pages(job)
        budget = await job.services.budgets.scope(
            client_id=job.client_id,
            daily_limit=None,
            run_id=job.run_id,
            tool_limit=job.services.settings.token.tool_llm_tokens_deep,
        )
        rows, tokens = [], 0
        spec = "\n".join(f"- {k}: {v}" for k, v in fields.items())
        for i, page_row in enumerate(pages):
            await job.raise_if_cancelled()
            prompt = (
                "Ekstrak data dari halaman berikut. Balas HANYA JSON objek dengan field:\n"
                f"{spec}\nGunakan null bila tidak ada. {instructions}\n\n"
                f'<untrusted_content source="{page_row.url[:200]}">\n'
                f"{_safe_md(page_row.content_md)}\n</untrusted_content>"
            )
            try:
                parsed, res = await llm.chat_json(
                    "extract", [{"role": "user", "content": prompt}], model, budget=budget
                )
                tokens += res.total_tokens
                data = parsed.model_dump()
            except ToolError as err:
                if err.code == "BUDGET_EXCEEDED":
                    raise
                data = {k: None for k in fields}
            rows.append({"url": page_row.url, **{k: _cell(data.get(k)) for k in fields}})
            await job.progress(i + 1, len(pages))
        cols = ["url", *fields]
        result = {
            "summary": f"{len(rows)} halaman diekstrak ({len(fields)} field), {tokens} token LLM",
            "total": len(rows),
            "items": {"cols": cols, "rows": [[r.get(c) for c in cols] for r in rows]},
            "stats": {},
            "cost": {"llm_tokens": tokens, "external_units": 0, "cache_hit": False},
        }
        return JobResult(result=result)


def _safe_md(content: str | None) -> str:
    return (content or "")[:EXTRACT_PAGE_CHARS].replace("<", "&lt;")


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        return truncate(value, 500)
    if isinstance(value, list | dict):
        return truncate(str(value), 500)
    return value


# login check per situs: url yang dibuka + penanda login/logout di URL akhir atau HTML
SITE_CHECKS: dict[str, dict[str, Any]] = {
    "x.com": {
        "url": "https://x.com/home",
        "out_url": ("/login", "/i/flow/login", "/logout"),
        "in_html": (
            'data-testid="SideNav_AccountSwitcher_Button"',
            'data-testid="AppTabBar_Home_Link"',
        ),
    },
    "threads.net": {
        "url": "https://www.threads.net/",
        "out_url": ("/login",),
        "in_html": ('aria-label="Create"', 'aria-label="Buat"', 'href="/activity"'),
    },
    "trends.google.com": {
        "url": "https://myaccount.google.com/",
        "out_url": ("ServiceLogin", "/signin"),
        "in_html": ("myaccount.google.com",),
    },
}
SITE_ALIASES = {
    "x": "x.com",
    "twitter.com": "x.com",
    "threads": "threads.net",
    "google": "trends.google.com",
    "trends": "trends.google.com",
}


def evaluate_login(site: str, final_url: str, html: str) -> tuple[bool, str]:
    rule = SITE_CHECKS[site]
    if any(marker in final_url for marker in rule["out_url"]):
        return False, "diarahkan ke halaman login"
    if any(marker in html for marker in rule["in_html"]):
        return True, "penanda akun terdeteksi"
    return False, "penanda login tidak ditemukan"


class BrowserProfileCheckHandler:
    kind = "browser_profile_check"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=10 * len(params.get("sites") or [1]))

    async def run(self, job: JobCtx) -> JobResult:
        name = job.params["profile"]
        profile = await profile_store(job.services).get(name)
        sites = [
            SITE_ALIASES.get(s, s) for s in job.params.get("sites") or profile.allowed_sites or []
        ]
        checks = await check_login(job, name, sites)
        return JobResult(result=_login_summary(name, checks))


async def check_login(job: JobCtx, name: str, sites: list[str]) -> list[dict[str, Any]]:
    """Buka halaman cek tiap situs (headless) lalu simpan `login_status` profil."""
    checks: list[dict[str, Any]] = []
    status: dict[str, dict[str, Any]] = {}
    async with launcher_for(job.services).session(name, job.job_id) as page:
        for site in sites:
            if site not in SITE_CHECKS:
                checks.append({"site": site, "logged_in": None, "detail": "situs tidak didukung"})
                continue
            fetched = await page.open(SITE_CHECKS[site]["url"])
            ok, detail = evaluate_login(site, fetched.final_url, fetched.html)
            checks.append({"site": site, "logged_in": ok, "detail": detail})
            status[site] = {"ok": ok, "checked_at": now().isoformat()}
    await profile_store(job.services).set_login_status(name, status)
    return checks


def _login_summary(name: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    logged = [str(c["site"]) for c in checks if c["logged_in"]]
    return {
        "summary": f"Profil {name}: login di {', '.join(logged) or 'tidak ada situs'}",
        "profile": name,
        "checks": checks,
    }


LOGIN_TIMEOUT_S = 900
LOGIN_POLL_S = 2.0


class BrowserProfileLoginHandler:
    """Login manual dari Dashboard: Chrome biasa terbuka di mesin MCP sampai user menutupnya.

    Profil dikunci selama jendela terbuka; setelah ditutup, status login dicek headless.
    """

    kind = "browser_profile_login"

    async def estimate(self, params: dict[str, Any]) -> JobEstimate:
        return JobEstimate(seconds=int(params.get("timeout_s") or LOGIN_TIMEOUT_S))

    async def run(self, job: JobCtx) -> JobResult:
        name = job.params["profile"]
        sites = [SITE_ALIASES.get(s, s) for s in job.params["sites"]]
        unknown = [s for s in sites if s not in LOGIN_URLS]
        if unknown:
            raise ValidationFailed(f"Situs login tidak didukung: {', '.join(unknown)}")
        timeout_s = float(job.params.get("timeout_s") or LOGIN_TIMEOUT_S)
        sleep = job.services.extras.get("sleep", asyncio.sleep)
        store = profile_store(job.services)
        profile = await store.acquire(name, job.job_id, wait_s=0)
        window = None
        deadline = time.monotonic() + timeout_s
        try:
            window = await asyncio.to_thread(
                opener_for(job.services), profile, [LOGIN_URLS[s] for s in sites]
            )
            await job.progress(0, None, "Jendela Chrome terbuka: login, lalu tutup jendelanya")
            while window.running() and time.monotonic() < deadline:
                await job.raise_if_cancelled()
                await sleep(LOGIN_POLL_S)
            closed_by_user = not window.running()
        finally:
            if window is not None and window.running():
                await asyncio.to_thread(window.close)
            await store.release(name, job.job_id)
        await store.allow_sites(name, sites)
        await job.progress(1, None, "Memeriksa status login")
        checks = await check_login(job, name, sites)
        return JobResult(result={**_login_summary(name, checks), "closed_by_user": closed_by_user})
