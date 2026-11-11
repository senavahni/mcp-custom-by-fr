"""Satu modul per toolset; impor modul = daftarkan tool ke registry."""

import importlib

TOOL_MODULES = (
    "toolbox",
    "data",
    "job",
    "pm",
    "pm_agent",
    "tpl",
    "task",
    "web",
    "social",
    "trends",
    "db",
    "kb",
    "analysis",
    "intel",
    "kn",
)
# modul tambahan untuk toolset yang sudah ada (bukan toolset baru di Admin Console)
EXTRA_MODULES = ("intel_keys",)


def load_all() -> None:
    for name in (*TOOL_MODULES, *EXTRA_MODULES):
        importlib.import_module(f"mcp_marketing.tools.{name}")
