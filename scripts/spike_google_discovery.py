"""Spike MCP-V1-06: ukur laju aman Google Search/Maps & frekuensi CAPTCHA di profil Chrome nyata.

    python scripts/spike_google_discovery.py --search 100 --maps 50        # spike penuh (±3–4 jam)
    python scripts/spike_google_discovery.py --search 5 --maps 3 --save-html  # uji coba singkat

- Profil khusus riset (default `DISCOVERY_PROFILE`, dibuat bila belum ada), Chrome selalu headful.
- Rem volume = pengaturan `DISCOVERY_*` di .env. Setiap navigasi dicatat ke
  `logs/spike/<ts>.jsonl`; ringkasan ke `logs/spike/<ts>.md`.
- CAPTCHA/consent: TIDAK diakali. Mode interaktif -> selesaikan di jendela Chrome, tekan Enter
  untuk lanjut (waktu tunggu dicatat). Non-interaktif (`--no-wait`) -> spike berhenti & melapor.
- `--save-html` menyimpan HTML mentah pertama per jenis halaman ke `logs/spike/` untuk
  memperbarui fixture `tests/fixtures/google_*.html` (periksa & bersihkan sebelum di-commit).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp_marketing.core.config import get_settings  # noqa: E402
from mcp_marketing.core.database import Database  # noqa: E402
from mcp_marketing.core.errors import CaptchaRequired, RateLimited, ToolError  # noqa: E402
from mcp_marketing.services.browser.driver_pool import (  # noqa: E402
    FetchedPage,
    PageSource,
    SeleniumLauncher,
)
from mcp_marketing.services.browser.profiles import ProfileStore  # noqa: E402
from mcp_marketing.services.collectors import google_discovery as gd  # noqa: E402

SEGMENTS = (
    "SMA swasta",
    "rumah sakit",
    "dinas pendidikan",
    "distributor alat kesehatan",
    "pabrik garmen",
    "universitas swasta",
    "klinik gigi",
    "perusahaan logistik",
    "koperasi simpan pinjam",
    "kantor notaris",
)
CITIES = (
    "Bandung",
    "Surabaya",
    "Semarang",
    "Yogyakarta",
    "Medan",
    "Makassar",
    "Denpasar",
    "Palembang",
    "Malang",
    "Bekasi",
)
LOCK_ID = "spike-discovery"


def default_queries() -> list[str]:
    return [f"{s} {c}" for c, s in itertools.product(CITIES, SEGMENTS)]


@dataclass
class Event:
    at: str
    kind: str
    query: str
    seconds: float
    results: int = 0
    with_website: int = 0
    with_phone: int = 0
    with_coords: int = 0
    block: str | None = None
    block_wait_s: float = 0.0
    error: str | None = None
    sample: list[dict[str, Any]] = field(default_factory=list)


class RecordingPage:
    """Bungkus PageSource: simpan HTML pertama per jenis halaman (opsional)."""

    def __init__(self, inner: PageSource, out: Path | None) -> None:
        self.inner, self.out, self.saved = inner, out, set[str]()

    async def open(self, url: str, *, wait_for: str | None = None) -> FetchedPage:
        page = await self.inner.open(url, wait_for=wait_for)
        if self.out is not None:
            name = (
                "search" if "/search?" in url and "/maps" not in url
                else "maps_place" if "/maps/place/" in page.final_url
                else "maps_feed"
            )  # fmt: skip
            if gd.detect_block(page.final_url, page.html):
                name = "blocked"
            if name not in self.saved:
                self.saved.add(name)
                (self.out / f"raw_{name}.html").write_text(page.html, encoding="utf-8")
        return page

    async def scroll(self, times: int = 1, pause_s: float = 1.0) -> None:
        await self.inner.scroll(times, pause_s)

    async def html(self) -> str:
        return await self.inner.html()

    async def current_url(self) -> str:
        return await self.inner.current_url()

    async def run_js(self, script: str, *args: Any) -> Any:
        return await self.inner.run_js(script, *args)


async def _wait_manual(wait: bool) -> float | None:
    """Tunggu user menyelesaikan verifikasi di Chrome; None = berhenti."""
    if not wait:
        return None
    started = time.monotonic()
    print("\n>>> Google meminta verifikasi. Selesaikan di jendela Chrome, lalu tekan Enter.")
    print(">>> Ketik 'stop' + Enter untuk mengakhiri spike.")
    answer = await asyncio.to_thread(input, "> ")
    return None if answer.strip().lower() == "stop" else time.monotonic() - started


async def _run_one(collector: gd.GoogleDiscovery, kind: str, query: str, lookups: int) -> Event:
    started = time.monotonic()
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    if kind == "google_search":
        results = await collector.search(query)
        return Event(
            at=at,
            kind=kind,
            query=query,
            seconds=round(time.monotonic() - started, 1),
            results=len(results),
            sample=[{"domain": r.domain, "title": r.title[:80]} for r in results[:5]],
        )
    found = await collector.maps_search(query, max_lookups=lookups)
    li = found.listings
    return Event(
        at=at,
        kind=kind,
        query=query,
        seconds=round(time.monotonic() - started, 1),
        results=len(found.feed),
        with_website=sum(1 for x in li if x.website),
        with_phone=sum(1 for x in li if x.phone),
        with_coords=sum(1 for x in li if x.lat is not None),
        sample=[{"name": x.name, "website": x.website, "has_phone": bool(x.phone)} for x in li],
    )


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    out = ROOT / "logs" / "spike"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path, report_path = out / f"{stamp}.jsonl", out / f"{stamp}.md"
    queries = (
        [
            q.strip()
            for q in Path(args.queries).read_text(encoding="utf-8").splitlines()
            if q.strip()
        ]
        if args.queries
        else default_queries()
    )
    plan = [("google_search", q) for q in queries[: args.search]] + [
        ("google_maps", q) for q in queries[: args.maps]
    ]
    db = Database(settings.db.database_url, pool_size=2)
    store = ProfileStore(db, settings.browser.browser_profiles_dir)
    events: list[Event] = []
    started = time.monotonic()
    try:
        if await store.find(args.profile) is None:
            await store.add(
                name=args.profile,
                purpose="Riset discovery Google Search/Maps (MCP-V1-06)",
                sites=["google.com"],
            )
            print(f"Profil {args.profile} dibuat (tanpa login).")
        launcher = SeleniumLauncher(settings, store)
        async with launcher.session(args.profile, LOCK_ID) as page:
            brake = gd.VolumeBrake(settings.discovery)
            collector = gd.GoogleDiscovery(
                RecordingPage(page, out if args.save_html else None),
                brake,
                blocked=gd.blocked_domains(settings.discovery),
            )
            i = 0
            while i < len(plan):
                kind, query = plan[i]
                print(f"[{i + 1}/{len(plan)}] {kind}: {query}", flush=True)
                try:
                    event = await _run_one(collector, kind, query, args.maps_lookups)
                    i += 1
                except CaptchaRequired as exc:
                    block = str((exc.details or {}).get("block"))
                    waited = await _wait_manual(not args.no_wait)
                    events.append(
                        Event(
                            at=datetime.now().astimezone().isoformat(timespec="seconds"),
                            kind=kind,
                            query=query,
                            seconds=0,
                            block=block,
                            block_wait_s=round(waited or 0, 1),
                        )
                    )
                    _append(log_path, events[-1])
                    if waited is None:
                        break
                    continue  # ulangi query yang sama setelah verifikasi
                except RateLimited as exc:
                    print(f"Kuota habis: {exc.message}")
                    break
                except ToolError as exc:
                    event = Event(
                        at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        kind=kind,
                        query=query,
                        seconds=0,
                        error=f"{exc.code}: {exc.message}",
                    )
                    i += 1
                events.append(event)
                _append(log_path, event)
    finally:
        await db.dispose()
    report = summarize(events, time.monotonic() - started, settings.discovery)
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Log: {log_path}\nRingkasan: {report_path}")
    return 0


def _append(path: Path, event: Event) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")


def summarize(events: list[Event], elapsed_s: float, cfg: Any) -> str:
    lines = [
        "# Spike Google Search/Maps (MCP-V1-06)",
        "",
        f"- Durasi total: {elapsed_s / 60:.1f} menit",
        f"- Rem: Search {cfg.discovery_search_per_hour}/jam,"
        f" Maps {cfg.discovery_maps_per_hour}/jam,"
        f" jeda {cfg.discovery_pause_min_s:.0f}–{cfg.discovery_pause_max_s:.0f} s",
        "",
        "| Jenis | Query OK | Blokir | Error | Rata hasil | Rata detik/query |",
        "|---|---|---|---|---|---|",
    ]
    for kind in ("google_search", "google_maps"):
        rows = [e for e in events if e.kind == kind]
        ok = [e for e in rows if not e.block and not e.error]
        blocks = [e for e in rows if e.block]
        errors = [e for e in rows if e.error]
        avg = statistics.mean(e.results for e in ok) if ok else 0
        sec = statistics.mean(e.seconds for e in ok) if ok else 0
        lines.append(
            f"| {kind} | {len(ok)} | {len(blocks)} | {len(errors)} | {avg:.1f} | {sec:.0f} |"
        )
    maps_ok = [e for e in events if e.kind == "google_maps" and not e.block and not e.error]
    opened = sum(len(e.sample) for e in maps_ok)
    if opened:
        lines += [
            "",
            f"Maps: {opened} tempat dibuka — website "
            f"{sum(e.with_website for e in maps_ok)}, telepon {sum(e.with_phone for e in maps_ok)},"
            f" koordinat {sum(e.with_coords for e in maps_ok)}",
        ]
    blocks = [e for e in events if e.block]
    if blocks:
        lines += ["", "## Blokir", ""] + [
            f"- {e.at} {e.kind} `{e.query}` -> {e.block} (tunggu manual {e.block_wait_s} s)"
            for e in blocks
        ]
    errors = [e for e in events if e.error]
    if errors:
        lines += ["", "## Error", ""] + [f"- {e.kind} `{e.query}`: {e.error}" for e in errors]
    return "\n".join(lines) + "\n"


def main() -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Spike discovery Google via Chrome (MCP-V1-06)")
    p.add_argument("--profile", default=settings.discovery.discovery_profile)
    p.add_argument("--search", type=int, default=100, help="jumlah query Google Search")
    p.add_argument("--maps", type=int, default=50, help="jumlah query Google Maps")
    p.add_argument("--maps-lookups", type=int, default=1, help="tempat dibuka per query Maps")
    p.add_argument("--queries", help="file teks: satu query per baris")
    p.add_argument("--save-html", action="store_true", help="simpan HTML mentah per jenis halaman")
    p.add_argument("--no-wait", action="store_true", help="berhenti saat CAPTCHA (non-interaktif)")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
