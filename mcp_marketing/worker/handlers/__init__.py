"""Registrasi handler job: pemeliharaan, web, sosial, Trends (TASK-14..17), KB (TASK-19), intel."""

from mcp_marketing.worker.handlers.base import HandlerRegistry
from mcp_marketing.worker.handlers.kb import KbIngestHandler, KbSyncDashboardHandler
from mcp_marketing.worker.handlers.maintenance import (
    CacheCleanupHandler,
    RetentionHandler,
    UsageRollupHandler,
)
from mcp_marketing.worker.handlers.sentiment import SentimentAnalyzeHandler
from mcp_marketing.worker.handlers.social import ThreadsSearchHandler, XSearchHandler
from mcp_marketing.worker.handlers.trends import (
    TrendsInterestHandler,
    TrendsRegionHandler,
    TrendsRelatedHandler,
)
from mcp_marketing.worker.handlers.web import (
    BrowserProfileCheckHandler,
    BrowserProfileLoginHandler,
    WebCrawlHandler,
    WebExtractHandler,
    WebScrapeHandler,
)


def default_registry() -> HandlerRegistry:
    # impor di sini: pipeline intel mengimpor submodul handler (hindari impor melingkar paket)
    from mcp_marketing.services.intel.pipeline import IntelPipelineHandler

    registry = HandlerRegistry()
    handlers = (
        CacheCleanupHandler(),
        RetentionHandler(),
        UsageRollupHandler(),
        WebScrapeHandler(),
        WebCrawlHandler(),
        WebExtractHandler(),
        BrowserProfileCheckHandler(),
        BrowserProfileLoginHandler(),
        XSearchHandler(),
        ThreadsSearchHandler(),
        TrendsInterestHandler(),
        TrendsRegionHandler(),
        TrendsRelatedHandler(),
        KbIngestHandler(),
        KbSyncDashboardHandler(),
        SentimentAnalyzeHandler(),
        IntelPipelineHandler(),
    )
    for handler in handlers:
        registry.register(handler)
    return registry
