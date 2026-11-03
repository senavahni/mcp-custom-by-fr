from mcp_marketing.core.config import get_settings
from mcp_marketing.server.auth import ClientIdentity
from mcp_marketing.server.registry import MAX_DESCRIPTION, MAX_SUMMARY, all_tools, compact_schema
from mcp_marketing.server.visibility import visible_tools
from mcp_marketing.tools import load_all


def test_all_tool_descriptions_within_limits() -> None:
    load_all()
    specs = all_tools()
    assert specs
    for spec in specs:
        assert len(spec.description) <= MAX_DESCRIPTION, spec.name
        assert len(spec.summary) <= MAX_SUMMARY, spec.name
        assert spec.input_schema()["type"] == "object"
        assert spec.output_schema()["type"] == "object"


def test_schema_has_no_auto_titles_but_keeps_title_property() -> None:
    schema = compact_schema(
        {
            "title": "X",
            "type": "object",
            "properties": {"title": {"title": "Title", "type": "string"}},
        }
    )
    assert "title" not in schema
    assert schema["properties"]["title"] == {"type": "string"}


def test_visibility_by_toolset_and_dev_flag() -> None:
    load_all()
    settings = get_settings()
    client = ClientIdentity(id="c", name="c", kind="external", toolsets=frozenset({"toolbox"}))
    names = {s.name for s in visible_tools(client, settings)}
    assert "toolbox_list" in names
    assert "data_get" not in names
    assert "toolbox_ping" in names  # ENABLE_DEV_TOOLS=true di test
    narrowed = {s.name for s in visible_tools(client, settings, frozenset({"data"}))}
    assert narrowed == set()
