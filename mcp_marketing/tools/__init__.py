"""Satu modul per toolset; impor modul = daftarkan tool ke registry."""

import importlib

TOOL_MODULES = (
    "toolbox",
    "data",
    "job",
    "pm",
    "pm_agent",
)


def load_all() -> None:
    for name in TOOL_MODULES:
        importlib.import_module(f"mcp_marketing.tools.{name}")
