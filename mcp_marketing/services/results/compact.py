"""Kompaksi output tool (MCP_SPEC §3 T1): pangkas bertahap sampai di bawah batas token.

Urutan mode `concise` (bila > soft): (a) teks per item -> `text_max_chars`, (b) items -> 5 baris,
(c) list di `stats` -> top-5, (d) hapus field `drop_first`. Bila masih > hard (mode apa pun):
potong keras + `truncated: true` + `hint`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from mcp_marketing.utils.text import truncate
from mcp_marketing.utils.tokens import count_tokens

SUMMARY_MAX = 300
TEXT_MAX_CHARS = 280
CONCISE_ITEMS = 5
STATS_TOP = 5
HARD_HINT = "Output dipotong; gunakan data_get(handle, …) untuk detail"


@dataclass(frozen=True)
class CompactResult:
    output: dict[str, Any]
    tokens_before: int
    tokens_after: int
    steps: tuple[str, ...]


def _truncate_strings(node: Any, max_chars: int) -> Any:
    if isinstance(node, str):
        return truncate(node, max_chars)
    if isinstance(node, list):
        return [_truncate_strings(v, max_chars) for v in node]
    if isinstance(node, dict):
        return {k: _truncate_strings(v, max_chars) for k, v in node.items()}
    return node


def _table(out: dict[str, Any]) -> dict[str, Any] | None:
    items = out.get("items")
    if isinstance(items, dict) and isinstance(items.get("rows"), list):
        return items
    return None


def _trim_stats(stats: Any, top: int) -> Any:
    if isinstance(stats, list):
        return [_trim_stats(v, top) for v in stats[:top]]
    if isinstance(stats, dict):
        return {k: _trim_stats(v, top) for k, v in stats.items()}
    return stats


def _largest_list(node: Any, path: tuple[Any, ...] = ()) -> tuple[tuple[Any, ...], int] | None:
    best: tuple[tuple[Any, ...], int] | None = None
    if isinstance(node, list):
        if len(node) > 1:
            best = (path, len(node))
        children: Any = enumerate(node)
    elif isinstance(node, dict):
        children = node.items()
    else:
        return None
    for key, value in children:
        found = _largest_list(value, (*path, key))
        if found and (best is None or found[1] > best[1]):
            best = found
    return best


def _get(node: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        node = node[key]
    return node


def clamp_summary(out: dict[str, Any]) -> None:
    summary = out.get("summary")
    if isinstance(summary, str) and len(summary) > SUMMARY_MAX:
        out["summary"] = truncate(summary, SUMMARY_MAX)


def compact(
    output: dict[str, Any],
    *,
    response_format: str = "concise",
    soft_tokens: int = 1_500,
    hard_tokens: int = 4_000,
    text_max_chars: int = TEXT_MAX_CHARS,
    drop_first: tuple[str, ...] = (),
) -> CompactResult:
    out = copy.deepcopy(output)
    clamp_summary(out)
    before = count_tokens(out)
    tokens = before
    steps: list[str] = []

    if response_format == "concise" and tokens > soft_tokens:
        table = _table(out)
        ladder: list[tuple[str, Any]] = [
            (
                "text",
                lambda: (
                    table and table.update(rows=_truncate_strings(table["rows"], text_max_chars))
                ),
            ),
            ("items", lambda: table and table.update(rows=table["rows"][:CONCISE_ITEMS])),
            (
                "stats",
                lambda: "stats" in out and out.update(stats=_trim_stats(out["stats"], STATS_TOP)),
            ),
            ("drop", lambda: [out.pop(k, None) for k in drop_first]),
        ]
        for name, action in ladder:
            if tokens <= soft_tokens:
                break
            action()
            steps.append(name)
            tokens = count_tokens(out)

    if tokens > hard_tokens:
        out = _truncate_strings(out, text_max_chars)
        clamp_summary(out)
        tokens = count_tokens(out)
        steps.append("hard")
        while tokens > hard_tokens:
            found = _largest_list(out)
            if found is None:
                # tidak ada list lagi: potong string lebih agresif
                out = _truncate_strings(out, 120)
                tokens = count_tokens(out)
                break
            path, size = found
            container = _get(out, path[:-1]) if path else None
            target = _get(out, path)
            keep = target[: max(1, size // 2)]
            if container is None:
                out = keep  # pragma: no cover - output root selalu dict
            else:
                container[path[-1]] = keep
            tokens = count_tokens(out)
        out["truncated"] = True
        out.setdefault("hint", HARD_HINT)
        tokens = count_tokens(out)
    return CompactResult(output=out, tokens_before=before, tokens_after=tokens, steps=tuple(steps))
