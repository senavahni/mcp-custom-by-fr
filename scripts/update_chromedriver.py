"""Unduh chromedriver (Chrome for Testing) yang cocok dengan Chrome terpasang.

python scripts/update_chromedriver.py [--force]

Versi Chrome dibaca dari CHROME_BINARY; build yang sama (major.minor.build) dicari di
`latest-patch-versions-per-build-with-downloads.json` lalu chromedriver.exe diekstrak ke
CHROMEDRIVER_PATH. Tidak memakai Selenium Manager (semua eksplisit dan bisa diaudit).
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from mcp_marketing.core.config import get_settings  # noqa: E402
from mcp_marketing.services.browser.driver_pool import (  # noqa: E402
    chrome_version,
    chromedriver_version,
)

CFT_INDEX = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "latest-patch-versions-per-build-with-downloads.json"
)
PLATFORM = "win64"
DOWNLOAD_TIMEOUT_S = 120


def driver_url(index: dict, chrome: str) -> tuple[str, str]:  # type: ignore[type-arg]
    build = ".".join(chrome.split(".")[:3])
    entry = (index.get("builds") or {}).get(build)
    if not entry:
        raise SystemExit(f"Chrome for Testing belum punya chromedriver untuk build {build}")
    for item in entry.get("downloads", {}).get("chromedriver", []):
        if item.get("platform") == PLATFORM:
            return str(entry["version"]), str(item["url"])
    raise SystemExit(f"Tidak ada chromedriver {PLATFORM} untuk build {build}")


def extract_driver(archive: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        member = next(n for n in zf.namelist() if n.endswith("chromedriver.exe"))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(zf.read(member))
        tmp.replace(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="unduh walau versi sudah cocok")
    args = parser.parse_args(argv)

    b = get_settings().browser
    chrome = chrome_version(b.chrome_binary)
    if not chrome:
        print(f"Chrome tidak ditemukan di {b.chrome_binary} (atur CHROME_BINARY)", file=sys.stderr)
        return 2
    current = chromedriver_version(b.chromedriver_path)
    print(f"Chrome       : {chrome}")
    print(f"chromedriver : {current or '(belum ada)'} di {b.chromedriver_path}")
    if current and current.split(".")[0] == chrome.split(".")[0] and not args.force:
        print("Versi major sudah cocok; tidak ada yang diunduh (pakai --force untuk paksa).")
        return 0

    with httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as http:
        index = http.get(CFT_INDEX).raise_for_status().json()
        version, url = driver_url(index, chrome)
        print(f"Mengunduh chromedriver {version} dari {url}")
        archive = http.get(url).raise_for_status().content
    extract_driver(archive, b.chromedriver_path)
    print(f"OK: {chromedriver_version(b.chromedriver_path)} -> {b.chromedriver_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
