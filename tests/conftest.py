"""Fixture bersama. Environment test diset sebelum modul aplikasi membaca konfigurasi."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

TEST_ENV = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "MCP_KEY_PEPPER": "test-pepper-0123456789abcdefghijklmnop",
    "MCP_SERVER_API_KEY": "test-server-api-key-0123456789abcdefghij",
    "MCP_WEBHOOK_SECRET": "test-webhook-secret-0123456789abcdefghij",
    "MCP_SERVICE_TOKEN": "test-service-token-0123456789abcdefghijk",
    "LLM_GATEWAY_URL": "http://gateway.test/v1",
    "LLM_GATEWAY_API_KEY": "lgw_test_key",
    "DASHBOARD_API_URL": "http://dashboard.test/api/v1",
    "ENABLE_DEV_TOOLS": "true",
}
# DATABASE_URL/MIGRATION_DATABASE_URL untuk test integrasi diambil dari TEST_DATABASE_URL (lihat
# tests/integration/conftest.py); unit test tidak membuka koneksi DB.
os.environ.update(TEST_ENV)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    from mcp_marketing.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
