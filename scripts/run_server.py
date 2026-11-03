"""Jalankan proses server MCP (uvicorn). Konfigurasi salah -> keluar dengan pesan nama variabel."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from mcp_marketing.core.config import ConfigError, get_settings  # noqa: E402
from mcp_marketing.main import create_app  # noqa: E402


def main() -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"GAGAL START: {exc}", file=sys.stderr)
        return 2
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_config=None,
        proxy_headers=False,
        timeout_graceful_shutdown=25,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
