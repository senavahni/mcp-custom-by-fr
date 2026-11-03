"""Pemecah batch agar event patuh batas kontrak (≤ 50 op blok / ≤ 900 KB, ≤ 20 subtask)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, TypeVar

import orjson

from mcp_marketing.schemas.contract import MAX_BLOCK_OPS, MAX_SUBTASKS

MAX_EVENT_BYTES = 900_000
T = TypeVar("T")


def chunk_block_ops(
    ops: Sequence[dict[str, Any]], *, max_ops: int = MAX_BLOCK_OPS, max_bytes: int = MAX_EVENT_BYTES
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for op in ops:
        op_size = len(orjson.dumps(op)) + 1
        if current and (len(current) >= max_ops or size + op_size > max_bytes):
            chunks.append(current)
            current, size = [], 0
        current.append(op)
        size += op_size
    if current:
        chunks.append(current)
    return chunks


def chunk_subtasks(subtasks: Sequence[T], *, size: int = MAX_SUBTASKS) -> list[list[T]]:
    return [list(subtasks[i : i + size]) for i in range(0, len(subtasks), size)]


def chain_append_ops(
    blocks: Sequence[dict[str, Any]], *, after_block_id: str | None
) -> list[dict[str, Any]]:
    """Ubah daftar blok menjadi operasi `append` berurutan.

    Bila `after_block_id` diisi (mis. prompt block), blok pertama disisip setelahnya dan blok
    berikutnya setelah blok sebelumnya (id dibuat di sisi MCP agar rantai stabil antar batch).
    """
    ops: list[dict[str, Any]] = []
    previous = after_block_id
    for block in blocks:
        block_id = block.get("id") or str(uuid.uuid4())
        op: dict[str, Any] = {"op": "append", "block": {**block, "id": block_id}}
        if previous is not None:
            op["after_block_id"] = previous
            previous = block_id
        ops.append(op)
    return ops
