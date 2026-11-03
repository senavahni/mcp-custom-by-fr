"""Convert read-only Dashboard BlockNote payloads into compact Markdown."""

from __future__ import annotations

from typing import Any


def _inline(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text", ""))
            styles = item.get("styles") or {}
            for key, marker in (("code", "`"), ("bold", "**"), ("italic", "*"), ("strike", "~~")):
                if styles.get(key):
                    text = f"{marker}{text}{marker}"
            parts.append(text)
        elif item.get("type") == "link":
            label = _inline(item.get("content"))
            parts.append(f"[{label}]({item.get('href', '')})")
    return "".join(parts)


def block_to_markdown(block_type: str, content_json: dict[str, Any]) -> str:
    props = content_json.get("props") or {}
    content = content_json.get("content")
    text = _inline(content)
    if block_type == "heading":
        return f"{'#' * int(props.get('level', 1))} {text}"
    if block_type == "bulletListItem":
        return f"- {text}"
    if block_type == "numberedListItem":
        return f"1. {text}"
    if block_type == "checkListItem":
        return f"- [{'x' if props.get('checked') else ' '}] {text}"
    if block_type == "quote":
        return f"> {text}"
    if block_type == "codeBlock":
        language = props.get("language") or ""
        return f"```{language}\n{text}\n```"
    if block_type == "divider":
        return "---"
    if block_type == "bookmark":
        title = props.get("title") or props.get("url") or "Link"
        return f"[{title}]({props.get('url', '')})"
    if block_type == "image":
        return f"![{props.get('caption') or ''}]({props.get('url', '')})"
    if block_type == "table":
        rows = (content or {}).get("rows", [])
        rendered = []
        for row in rows:
            cells = [
                _inline(cell if isinstance(cell, list) else cell.get("content"))
                for cell in row.get("cells", [])
            ]
            rendered.append("| " + " | ".join(cells) + " |")
        if rendered:
            rendered.insert(1, "| " + " | ".join("---" for _ in rows[0].get("cells", [])) + " |")
        return "\n".join(rendered)
    return text


def blocks_to_markdown(blocks: list[Any]) -> str:
    """Render blocks in their provided order; callers supply rows ordered by order_index."""
    lines: list[str] = []
    for block in blocks:
        block_type = getattr(block, "block_type", None) or block.get("block_type")
        content_json = getattr(block, "content_json", None) or block.get("content_json", {})
        lines.append(block_to_markdown(str(block_type), content_json))
    return "\n\n".join(line for line in lines if line)
