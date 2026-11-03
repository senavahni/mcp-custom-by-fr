"""Driver Chrome (Selenium) dengan chromedriver EKSPLISIT; Selenium Manager tidak dipakai.

Semua panggilan Selenium (blocking) dijalankan di thread. `PageSource` adalah antarmuka minimal
yang dipakai handler (scrape/crawl/X/Threads/Trends) sehingga bisa dites tanpa Chrome nyata.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp_marketing.core.config import Settings
from mcp_marketing.core.errors import UpstreamError, UpstreamTimeout
from mcp_marketing.core.logging import get_logger
from mcp_marketing.models.browser import BrowserProfile
from mcp_marketing.services.browser.profiles import ProfileStore

log = get_logger(__name__)

UPDATE_SCRIPT = "scripts\\update-chromedriver.bat"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")
NAV_STATUS_JS = (
    "const e = performance.getEntriesByType('navigation')[0];"
    "return e && e.responseStatus ? e.responseStatus : null;"
)


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int | None
    title: str | None
    html: str


class PageSource(Protocol):
    async def open(self, url: str, *, wait_for: str | None = None) -> FetchedPage: ...

    async def scroll(self, times: int = 1, pause_s: float = 1.0) -> None: ...

    async def html(self) -> str: ...

    async def current_url(self) -> str: ...

    async def run_js(self, script: str, *args: Any) -> Any: ...


class BrowserLauncher(Protocol):
    def session(
        self, profile: str, job_id: str
    ) -> contextlib.AbstractAsyncContextManager[PageSource]: ...


# ---------------------------------------------------------------------- versi
def chrome_version(chrome_binary: Path) -> str | None:
    """Versi Chrome tanpa membuka jendela: folder versi di samping chrome.exe (Windows)."""
    if chrome_binary.parent.exists():
        versions = sorted(
            (p.name for p in chrome_binary.parent.iterdir() if _VERSION_RE.fullmatch(p.name)),
            key=lambda v: tuple(int(x) for x in v.split(".")),
        )
        if versions:
            return versions[-1]
    try:
        out = subprocess.run(
            [str(chrome_binary), "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(out)
    return match.group(0) if match else None


def chromedriver_version(driver_path: Path) -> str | None:
    if not driver_path.exists():
        return None
    try:
        out = subprocess.run(
            [str(driver_path), "--version"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(out)
    return match.group(0) if match else None


def check_versions(settings: Settings) -> dict[str, Any]:
    """Cocokkan major version Chrome vs chromedriver; pesan berisi langkah perbaikan."""
    b = settings.browser
    chrome = chrome_version(b.chrome_binary)
    driver = chromedriver_version(b.chromedriver_path)
    ok = bool(chrome and driver and chrome.split(".")[0] == driver.split(".")[0])
    hint = None
    if not driver:
        hint = f"chromedriver tidak ditemukan di {b.chromedriver_path}; jalankan {UPDATE_SCRIPT}"
    elif not chrome:
        hint = f"Chrome tidak ditemukan di {b.chrome_binary} (CHROME_BINARY)"
    elif not ok:
        hint = (
            f"Versi tidak cocok (Chrome {chrome} vs chromedriver {driver}); "
            f"jalankan {UPDATE_SCRIPT}"
        )
    return {"ok": ok, "chrome": chrome, "chromedriver": driver, "hint": hint}


def require_compatible(settings: Settings) -> None:
    status = check_versions(settings)
    if not status["ok"]:
        raise UpstreamError(
            f"Browser belum siap: {status['hint']}", retryable=False, details=status
        )


# ---------------------------------------------------------------------- driver
def chrome_options(profile: BrowserProfile, settings: Settings) -> Any:
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.binary_location = str(settings.browser.chrome_binary)
    opts.add_argument(f"--user-data-dir={profile.user_data_dir}")
    opts.add_argument(f"--profile-directory={profile.profile_directory}")
    opts.add_argument(f"--window-size={profile.window_size}")
    opts.add_argument("--lang=id-ID")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    if profile.headless or settings.browser.browser_headless:
        opts.add_argument("--headless=new")
    if profile.user_agent:
        opts.add_argument(f"--user-agent={profile.user_agent}")
    if profile.proxy_url_env:
        proxy = os.environ.get(profile.proxy_url_env)
        if not proxy:
            raise UpstreamError(f"Env proxy {profile.proxy_url_env} kosong", retryable=False)
        opts.add_argument(f"--proxy-server={proxy}")
    return opts


def create_driver(profile: BrowserProfile, settings: Settings) -> Any:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    log_path = settings.log_dir / "chromedriver.log"
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    service = Service(
        executable_path=str(settings.browser.chromedriver_path), log_output=str(log_path)
    )
    driver = webdriver.Chrome(service=service, options=chrome_options(profile, settings))
    driver.set_page_load_timeout(settings.browser.browser_page_timeout_s)
    return driver


def kill_driver(driver: Any) -> None:
    """quit(); bila gagal/hang, matikan proses chromedriver beserta anak (Chrome)."""
    try:
        driver.quit()
        return
    except Exception as exc:  # noqa: BLE001 - lanjut ke kill paksa
        log.warning("browser.quit_failed", error=type(exc).__name__)
    process = getattr(getattr(driver, "service", None), "process", None)
    if process is not None and process.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
        )


class SeleniumPage:
    def __init__(self, driver: Any, settings: Settings) -> None:
        self.driver = driver
        self.settings = settings

    async def open(self, url: str, *, wait_for: str | None = None) -> FetchedPage:
        def _go() -> FetchedPage:
            from selenium.common.exceptions import TimeoutException, WebDriverException
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as ec
            from selenium.webdriver.support.ui import WebDriverWait

            try:
                self.driver.get(url)
                if wait_for:
                    WebDriverWait(self.driver, self.settings.browser.browser_page_timeout_s).until(
                        ec.presence_of_element_located((By.CSS_SELECTOR, wait_for))
                    )
            except TimeoutException:
                raise UpstreamTimeout(f"Halaman tidak selesai dimuat: {url[:200]}") from None
            except WebDriverException as exc:
                raise UpstreamError(
                    f"Browser gagal membuka halaman ({type(exc).__name__})"
                ) from None
            status = None
            with contextlib.suppress(Exception):
                status = self.driver.execute_script(NAV_STATUS_JS)
            return FetchedPage(
                url=url,
                final_url=self.driver.current_url,
                status=status,
                title=self.driver.title or None,
                html=self.driver.page_source,
            )

        return await asyncio.to_thread(_go)

    async def scroll(self, times: int = 1, pause_s: float = 1.0) -> None:
        for _ in range(max(0, times)):
            await asyncio.to_thread(
                self.driver.execute_script, "window.scrollBy(0, document.body.scrollHeight);"
            )
            await asyncio.sleep(pause_s)

    async def html(self) -> str:
        return str(await asyncio.to_thread(lambda: self.driver.page_source))

    async def current_url(self) -> str:
        return str(await asyncio.to_thread(lambda: self.driver.current_url))

    async def run_js(self, script: str, *args: Any) -> Any:
        return await asyncio.to_thread(self.driver.execute_script, script, *args)


class SeleniumLauncher:
    """Satu driver per job; profil dikunci selama sesi dan driver selalu ditutup di `finally`."""

    def __init__(self, settings: Settings, profiles: ProfileStore) -> None:
        self.settings = settings
        self.profiles = profiles

    @contextlib.asynccontextmanager
    async def session(self, profile: str, job_id: str) -> AsyncIterator[PageSource]:
        require_compatible(self.settings)
        record = await self.profiles.acquire(profile, job_id)
        driver = None
        started = time.monotonic()
        try:
            driver = await asyncio.to_thread(create_driver, record, self.settings)
            log.info("browser.session_start", profile=profile)
            yield SeleniumPage(driver, self.settings)
        finally:
            if driver is not None:
                await asyncio.to_thread(kill_driver, driver)
            await self.profiles.release(profile, job_id)
            log.info(
                "browser.session_end", profile=profile, seconds=round(time.monotonic() - started, 1)
            )


def launcher_for(services: Any) -> BrowserLauncher:
    """Launcher dari `services.extras` (test bisa menyuntik launcher palsu)."""
    launcher = services.extras.get("browser_launcher")
    if launcher is None:
        launcher = SeleniumLauncher(services.settings, profile_store(services))
        services.extras["browser_launcher"] = launcher
    return launcher  # type: ignore[no-any-return]


def profile_store(services: Any) -> ProfileStore:
    store = services.extras.get("profile_store")
    if store is None:
        store = ProfileStore(services.db, services.settings.browser.browser_profiles_dir)
        services.extras["profile_store"] = store
    return store  # type: ignore[no-any-return]
