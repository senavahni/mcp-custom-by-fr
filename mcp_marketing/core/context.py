"""Konteks request (contextvars) yang mengalir otomatis ke log dan header panggilan keluar."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

CONTEXT_FIELDS = ("request_id", "client_id", "client_name", "run_id", "job_id", "tool")

_vars: dict[str, ContextVar[str | None]] = {
    name: ContextVar(f"mcp_{name}", default=None) for name in CONTEXT_FIELDS
}


def get(name: str) -> str | None:
    return _vars[name].get()


def snapshot() -> dict[str, str]:
    """Nilai konteks yang terisi (untuk log)."""
    return {name: value for name, var in _vars.items() if (value := var.get()) is not None}


def bind_context(**values: Any) -> dict[str, Token[str | None]]:
    """Set nilai konteks; kembalikan token untuk `reset_context`. Nilai None menghapus field."""
    tokens: dict[str, Token[str | None]] = {}
    for name, value in values.items():
        if name not in _vars:
            raise KeyError(f"field konteks tidak dikenal: {name}")
        tokens[name] = _vars[name].set(None if value is None else str(value))
    return tokens


def reset_context(tokens: dict[str, Token[str | None]]) -> None:
    for name, token in tokens.items():
        _vars[name].reset(token)


@contextmanager
def context_scope(**values: Any) -> Iterator[None]:
    tokens = bind_context(**values)
    try:
        yield
    finally:
        reset_context(tokens)


def outgoing_headers() -> dict[str, str]:
    """Header korelasi untuk Gateway/Dashboard."""
    request_id = get("request_id")
    return {"X-Request-Id": request_id} if request_id else {}
