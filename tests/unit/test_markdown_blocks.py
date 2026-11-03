from __future__ import annotations

from mcp_marketing.services.templates.md_to_blocks import markdown_to_blocks


def test_markdown_supports_bookmarks_styles_checklists_tables_and_code() -> None:
    blocks = markdown_to_blocks(
        "\n".join(
            [
                "## Ringkasan **utama**",
                "- [x] Selesai",
                "[Dokumentasi](https://example.com)",
                "| Nama | Nilai |",
                "|---|---:|",
                "| A | 1 |",
                "```python",
                "print('ok')",
                "```",
            ]
        )
    )

    assert [block["block_type"] for block in blocks] == [
        "heading",
        "checkListItem",
        "bookmark",
        "table",
        "codeBlock",
    ]
    assert blocks[0]["content_json"]["content"][1]["styles"]["bold"] is True
    assert blocks[2]["content_json"]["props"]["url"] == "https://example.com"


def test_markdown_rejects_tables_exceeding_column_limit() -> None:
    row = "| " + " | ".join(f"C{i}" for i in range(21)) + " |"
    separator = "| " + " | ".join("---" for _ in range(21)) + " |"

    try:
        markdown_to_blocks(f"{row}\n{separator}")
    except ValueError as exc:
        assert "kolom" in str(exc)
    else:
        raise AssertionError("Tabel lebih dari 20 kolom seharusnya ditolak")
