"""Registrasi handler job bawaan. Handler crawl/scrape/analisis ditambahkan di TASK-14+."""

from mcp_marketing.worker.handlers.base import HandlerRegistry
from mcp_marketing.worker.handlers.maintenance import (
    CacheCleanupHandler,
    RetentionHandler,
    UsageRollupHandler,
)


def default_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    for handler in (CacheCleanupHandler(), RetentionHandler(), UsageRollupHandler()):
        registry.register(handler)
    return registry
