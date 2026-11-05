"""Jendela login manual untuk profil browser MCP (dipicu dari Dashboard).

Chrome dibuka sebagai proses biasa (tanpa chromedriver/otomasi) dengan `--user-data-dir` profil:
Google sering menolak login di browser yang dikendalikan otomasi. Password tidak pernah melewati
MCP; sesi login tersimpan di folder profil seperti Chrome pada umumnya.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any, Protocol

from mcp_marketing.core.config import Settings
from mcp_marketing.models.browser import BrowserProfile

CLOSE_GRACE_S = 10

# halaman login per situs (kunci = situs kanonik SITE_CHECKS)
LOGIN_URLS: dict[str, str] = {
    "trends.google.com": "https://accounts.google.com/",
    "x.com": "https://x.com/login",
    "threads.net": "https://www.threads.net/login",
}


class LoginWindow(Protocol):
    def running(self) -> bool: ...

    def close(self) -> None: ...


Opener = Callable[[BrowserProfile, list[str]], LoginWindow]


class ChromeLoginWindow:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def running(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        """Tutup wajar dulu (cookie tersimpan), paksa bila tidak merespons."""
        if not self.running():
            return
        pid = str(self.process.pid)
        subprocess.run(["taskkill", "/PID", pid, "/T"], capture_output=True, check=False)
        try:
            self.process.wait(timeout=CLOSE_GRACE_S)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, check=False)


def chrome_login_command(settings: Settings, profile: BrowserProfile, urls: list[str]) -> list[str]:
    return [
        str(settings.browser.chrome_binary),
        f"--user-data-dir={profile.user_data_dir}",
        f"--profile-directory={profile.profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        *urls,
    ]


def opener_for(services: Any) -> Opener:
    """Pembuka jendela dari `services.extras` (test menyuntik jendela palsu)."""
    opener = services.extras.get("login_window_opener")
    if opener is not None:
        return opener  # type: ignore[no-any-return]

    def open_chrome(profile: BrowserProfile, urls: list[str]) -> LoginWindow:
        cmd = chrome_login_command(services.settings, profile, urls)
        return ChromeLoginWindow(subprocess.Popen(cmd))  # noqa: S603 - argumen dari konfigurasi

    return open_chrome
