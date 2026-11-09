"""Proses worker: job queue + scheduler. Ctrl+C -> graceful shutdown (job kembali ke antrean)."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_marketing.core.config import ConfigError, get_settings  # noqa: E402
from mcp_marketing.core.database import set_database  # noqa: E402
from mcp_marketing.core.logging import configure_logging, get_logger  # noqa: E402
from mcp_marketing.main import build_services, check_pgvector, close_services  # noqa: E402
from mcp_marketing.services.browser.driver_pool import check_versions  # noqa: E402
from mcp_marketing.worker.handlers import default_registry  # noqa: E402
from mcp_marketing.worker.runner import Worker  # noqa: E402
from mcp_marketing.worker.scheduler import Scheduler  # noqa: E402


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    browser = check_versions(settings)
    if not browser["ok"]:
        # job non-browser tetap jalan; job browser akan gagal dengan pesan yang sama
        get_logger("worker").critical("browser.not_ready", **browser)
    services = build_services(settings)
    set_database(services.db)
    try:
        await check_pgvector(services)
    except ConfigError:
        await close_services(services)
        raise
    registry = default_registry()
    assert services.jobs is not None
    services.jobs.estimator = registry
    worker = Worker(
        services,
        services.jobs,
        registry,
        concurrency=settings.worker.worker_concurrency,
        poll_interval_s=settings.worker.worker_poll_interval_ms / 1000,
        scheduler=Scheduler(services.db, services.jobs, timezone=settings.timezone),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows: Ctrl+C -> KeyboardInterrupt
            pass
    try:
        await worker.run()
    finally:
        await close_services(services)


def main() -> int:
    try:
        get_settings()
    except ConfigError as exc:
        print(f"GAGAL START: {exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except ConfigError as exc:
        print(f"GAGAL START: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
