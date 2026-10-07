import asyncio

from dspace_mcp.server import mcp


def _tools():
    return asyncio.run(mcp.list_tools())


def test_tool_count_and_names():
    tools = _tools()
    names = {t.name for t in tools}
    assert len(tools) == 33
    for n in ("saf_build_from_csv", "saf_import", "dspace_ping", "raw_sql", "harvest_by_affiliation"):
        assert n in names


def test_tools_have_descriptions():
    for t in _tools():
        assert (t.description or "").strip(), t.name
