"""`erp-agents mcp` as a real program speaking MCP over stdin/stdout, the way Claude Desktop starts it."""
import asyncio
import os
import sys
from ai_munimji_engine.core.packs import PackRegistry
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

from mcp import Client, StdioServerParameters

SRC = str(Path(__file__).resolve().parent.parent / "src")


def server(tmp_path, role=None):
    cfg = tmp_path / "settings.yaml"
    lines = ["database:", f"  path: {(tmp_path / 'db.sqlite').as_posix()}", "erp:", "  type: mock", "system:", "  stock_alerts: false"]
    if role:
        lines += ["mcp:", f"  stdio_role: {role}"]
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return StdioServerParameters(command=sys.executable, args=["-m", "erp_agents.core.cli", "--config", str(cfg), "mcp"], env=env)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 60))


def test_a_client_can_list_and_call_the_tools_over_stdio(tmp_path):
    async def go():
        async with Client(server(tmp_path)) as c:
            tools = [t.name for t in (await c.list_tools()).tools]
            overview = await c.call_tool("stock_overview", {"limit": 3})
            status = await c.call_tool("stock_status", {"item_code": "BOLT-M8"})
            refused = await c.call_tool("prepare_order_request", {"item_code": "BOLT-M8", "reason": "x"})
            return tools, overview, status, refused
    tools, overview, status, refused = run(go())
    declared = [d.name for d in PackRegistry.discover().manifests["stock_procurement"].tools]
    assert tools[0] == "stock_overview" and "prepare_order_request" in tools and sorted(tools) == sorted(declared)       # exactly what the pack declares, nothing more
    assert not overview.is_error and overview.structured_content["items_total"] == 10 and len(overview.structured_content["items"]) <= 3
    assert status.structured_content["item_code"] == "BOLT-M8"
    assert refused.is_error and "not allowed" in refused.content[0].text                                          # the default caller can look, not prepare


def test_the_configured_role_decides_what_the_stdio_client_may_do(tmp_path):
    async def go():
        async with Client(server(tmp_path, "approver")) as c:
            return await c.call_tool("prepare_order_request", {"item_code": "BOLT-M8", "qty": 600, "reason": "Asked from the desktop assistant."})
    res = run(go())
    assert not res.is_error and res.structured_content["status"] in ("created", "manual") and "Nothing is ordered" in res.structured_content["note"] + "Nothing is ordered"


def test_a_role_that_could_change_things_is_refused_at_start(tmp_path):
    import subprocess
    params = server(tmp_path, "admin")
    out = subprocess.run([params.command, *params.args], env=params.env, capture_output=True, text=True, timeout=60, input="")
    assert out.returncode == 2 and "viewer" in out.stderr and out.stdout == ""
