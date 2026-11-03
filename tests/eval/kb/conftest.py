"""Eval KB memakai fixture database dari test integrasi (butuh TEST_ADMIN_DATABASE_URL)."""

import os
from pathlib import Path

import pytest

from tests.integration.conftest import clean_db, db, prepared_db, services  # noqa: F401


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # hook ini dipanggil untuk SEMUA item sesi -> batasi ke folder ini saja
    if os.environ.get("TEST_ADMIN_DATABASE_URL"):
        return
    here = str(Path(__file__).parent)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.skip(reason="TEST_ADMIN_DATABASE_URL tidak diset"))
