"""TASK-14: cek versi Chrome/chromedriver, opsi Chrome per profil, dan transport Trends via browser."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.services.browser import driver_pool
from mcp_marketing.services.collectors.trends_base import TrendsRateLimited
from mcp_marketing.services.collectors.trends_google import BrowserTransport, GoogleTrendsClient


def _settings(tmp_path: Path, driver_major: str | None) -> Any:
    chrome = tmp_path / "chrome" / "chrome.exe"
    (chrome.parent / "131.0.6778.85").mkdir(parents=True)
    driver = tmp_path / "bin" / "chromedriver.bat"
    if driver_major:
        driver.parent.mkdir(parents=True)
        driver.write_text(f"@echo ChromeDriver {driver_major}.0.6778.85 (abc)\n", encoding="utf-8")
    browser = SimpleNamespace(chrome_binary=chrome, chromedriver_path=driver)
    return SimpleNamespace(browser=browser)


def test_check_versions_ok(tmp_path: Path) -> None:
    status = driver_pool.check_versions(_settings(tmp_path, "131"))
    assert status == {
        "ok": True,
        "chrome": "131.0.6778.85",
        "chromedriver": "131.0.6778.85",
        "hint": None,
    }
    driver_pool.require_compatible(_settings(tmp_path / "again", "131"))


def test_check_versions_mismatch_and_missing(tmp_path: Path) -> None:
    mismatch = driver_pool.check_versions(_settings(tmp_path / "a", "130"))
    assert not mismatch["ok"] and "update-chromedriver" in mismatch["hint"]
    missing = _settings(tmp_path / "b", None)
    assert "tidak ditemukan" in driver_pool.check_versions(missing)["hint"]
    with pytest.raises(UpstreamError):
        driver_pool.require_compatible(missing)


def _profile(**over: Any) -> Any:
    base = {
        "user_data_dir": r"C:\profiles\riset",
        "profile_directory": "Default",
        "window_size": "1366,900",
        "headless": False,
        "user_agent": None,
        "proxy_url_env": None,
    }
    return SimpleNamespace(**(base | over))


def _opt_settings(headless: bool = False) -> Any:
    browser = SimpleNamespace(chrome_binary=Path("chrome.exe"), browser_headless=headless)
    return SimpleNamespace(browser=browser)


def test_chrome_options_profile_and_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    opts = driver_pool.chrome_options(_profile(headless=True, user_agent="UA"), _opt_settings())
    assert r"--user-data-dir=C:\profiles\riset" in opts.arguments
    assert "--headless=new" in opts.arguments and "--user-agent=UA" in opts.arguments
    monkeypatch.setenv("RISET_PROXY", "http://proxy.local:8080")
    proxied = driver_pool.chrome_options(_profile(proxy_url_env="RISET_PROXY"), _opt_settings())
    assert "--proxy-server=http://proxy.local:8080" in proxied.arguments
    monkeypatch.delenv("RISET_PROXY")
    with pytest.raises(UpstreamError):
        driver_pool.chrome_options(_profile(proxy_url_env="RISET_PROXY"), _opt_settings())


def test_kill_driver_quit_and_fallback() -> None:
    quit_calls: list[int] = []
    ok = SimpleNamespace(quit=lambda: quit_calls.append(1))
    driver_pool.kill_driver(ok)

    def boom() -> None:
        raise RuntimeError("hang")

    exited = SimpleNamespace(poll=lambda: 0, pid=1)
    driver_pool.kill_driver(SimpleNamespace(quit=boom, service=SimpleNamespace(process=exited)))
    assert quit_calls == [1]


class XhrPage:
    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = responses
        self.opened: list[str] = []
        self.scripts: list[str] = []

    async def open(self, url: str, *, wait_for: str | None = None) -> Any:
        self.opened.append(url)

    async def run_js(self, script: str, *args: Any) -> Any:
        self.scripts.append(str(args[0]))
        return self.responses.pop(0)


async def test_browser_transport_runs_xhr_in_tab() -> None:
    explore = {"widgets": [{"id": "RELATED_QUERIES", "request": {}, "token": "t"}]}
    related = {"default": {"rankedList": [{"rankedKeyword": [{"query": "kopi", "value": 9}]}]}}
    page = XhrPage(
        [(200, ")]}'\n" + orjson.dumps(explore).decode()), (200, orjson.dumps(related).decode())]
    )

    async def no_sleep(_s: float) -> None:
        return None

    client = GoogleTrendsClient(
        BrowserTransport(page, "https://trends.google.com"), name="browser", sleep=no_sleep
    )
    res = await client.related("kopi", date(2026, 1, 1), date(2026, 2, 1), "ID", "queries")
    assert res.top[0].term == "kopi"
    assert page.opened == ["https://trends.google.com/trends/explore?geo=ID&hl=id"]
    assert page.scripts[0].startswith("/trends/api/explore?")


async def test_browser_transport_status_errors() -> None:
    transport = BrowserTransport(XhrPage([(429, ""), (500, "")]), "https://trends.google.com")
    with pytest.raises(TrendsRateLimited):
        await transport.get_text("/trends/api/explore", {})
    with pytest.raises(UpstreamError):
        await transport.get_text("/trends/api/explore", {})
