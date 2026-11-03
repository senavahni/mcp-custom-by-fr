"""Registrasi handler job: pemeliharaan, web (TASK-14), sosial (TASK-15/16), Trends (TASK-17)."""

from mcp_marketing.worker.handlers.base import HandlerRegistry
from mcp_marketing.worker.handlers.maintenance import (
    CacheCleanupHandler,
    RetentionHandler,
    UsageRollupHandler,
)
from mcp_marketing.worker.handlers.social import ThreadsSearchHandler, XSearchHandler
from mcp_marketing.worker.handlers.trends import (
    TrendsInterestHandler,
    TrendsRegionHandler,
    TrendsRelatedHandler,
)
from mcp_marketing.worker.handlers.web import (
    BrowserProfileCheckHandler,
    WebCrawlHandler,
    WebExtractHandler,
    WebScrapeHandler,
)


def default_registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    handlers = (
        CacheCleanupHandler(),
        RetentionHandler(),
        UsageRollupHandler(),
        WebScrapeHandler(),
        WebCrawlHandler(),
        WebExtractHandler(),
        BrowserProfileCheckHandler(),
        XSearchHandler(),
        ThreadsSearchHandler(),
        TrendsInterestHandler(),
        TrendsRegionHandler(),
        TrendsRelatedHandler(),
    )
    for handler in handlers:
        registry.register(handler)
    return registry
