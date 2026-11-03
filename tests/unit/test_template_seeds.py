from __future__ import annotations

from datetime import date

from mcp_marketing.services.templates.library import SEED_DIR, load_seed
from mcp_marketing.services.templates.renderer import render
from mcp_marketing.utils.tokens import count_tokens


def _sample_variables(template: dict) -> dict:
    values = {}
    for spec in template.get("variables", []):
        if "default" in spec:
            values[spec["name"]] = spec["default"]
        elif spec.get("required"):
            values[spec["name"]] = {
                "date": "2026-11-02",
                "list": ["Instagram", "TikTok"],
            }.get(spec.get("type"), "Contoh")
    return values


def test_all_eight_seed_templates_render_to_validated_blocks() -> None:
    seeds = [load_seed(path) for path in sorted(SEED_DIR.glob("*.yaml"))]

    assert len(seeds) == 8
    for template in seeds:
        rendered = render(
            template,
            _sample_variables(template),
            date(2026, 11, 2),
        )
        assert rendered.blocks
        assert all(block["block_type"] and block["content_json"] for block in rendered.blocks)
        assert count_tokens(rendered.outline) <= 300
