"""Прямая проверка MCP-сервера по stdio без LLM: initialize + list_tools + вызов локальных инструментов.
Запуск: uv --directory ../.. run python demo/opencode/smoke_test.py
"""
import asyncio, os, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

async def main() -> int:
    params = StdioServerParameters(
        command="uv",
        args=["--directory", str(REPO), "run", "dspace-mcp"],
        env={**os.environ, "DSPACE_MCP_CONFIG": str(HERE / "config.demo.toml"), "DSPACE_MCP_PROFILE": "demo"},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"tools: {len(tools)}")
            print(", ".join(t.name for t in tools))
            res = await s.call_tool("saf_build_from_csv", {"csv_path": str(HERE / "data" / "articles.csv")})
            print("saf_build_from_csv:", res.content[0].text)
            res = await s.call_tool("saf_import", {"profile": "demo", "local_saf_dir": str(HERE / "data" / "SimpleArchiveFormat"),
                                                   "collection_handle": "123456789/1", "confirm": False})
            print("saf_import(confirm=False):", res.content[0].text)
            res = await s.call_tool("dspace_ping", {"profile": "demo"})
            print("dspace_ping:", res.content[0].text[:600])
    return 0

sys.exit(asyncio.run(main()))
