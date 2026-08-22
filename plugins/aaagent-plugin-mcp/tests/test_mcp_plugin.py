from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aaagent.core.tool_registry import ToolRegistry
from aaagent_plugin_mcp import McpToolsPlugin

_SERVER = Path(__file__).parent / "mcp_test_server.py"


def _server_cfg() -> dict:
    return {
        "mcp": {
            "servers": [
                {
                    "name": "demo",
                    "transport": "stdio",
                    "command": [sys.executable, str(_SERVER)],
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_mcp_tools_expanded_and_callable():
    cfg = _server_cfg()
    plugin = McpToolsPlugin()
    reg = ToolRegistry(allowed_dirs=[])
    plugin.register(reg, cfg)
    assert len(plugin._sessions) == 1

    await plugin.establish(reg, cfg)

    names = set(reg.tool_names)
    assert "demo_add" in names
    assert "demo_echo" in names

    add_result = await reg.execute("demo_add", '{"a": 2, "b": 3}')
    assert "5" in add_result

    echo_result = await reg.execute("demo_echo", '{"text": "hello mcp"}')
    assert "hello mcp" in echo_result

    await plugin.close()


@pytest.mark.asyncio
async def test_mcp_missing_server_is_isolation():
    cfg = {
        "mcp": {
            "servers": [
                {"name": "bad", "transport": "stdio", "command": ["no-such-exe-xyz"]},
                *(_server_cfg()["mcp"]["servers"]),
            ]
        }
    }
    plugin = McpToolsPlugin()
    reg = ToolRegistry(allowed_dirs=[])
    plugin.register(reg, cfg)
    await plugin.establish(reg, cfg)

    assert "bad_" not in set(reg.tool_names)
    assert "demo_add" in set(reg.tool_names)
    await plugin.close()


@pytest.mark.asyncio
async def test_mcp_disabled_server_not_registered():
    cfg = {
        "mcp": {
            "servers": [
                {"name": "demo", "transport": "stdio",
                 "command": [sys.executable, str(_SERVER)], "enabled": False},
            ]
        }
    }
    plugin = McpToolsPlugin()
    reg = ToolRegistry(allowed_dirs=[])
    plugin.register(reg, cfg)
    assert plugin._sessions == []
    await plugin.establish(reg, cfg)
    assert "demo_add" not in set(reg.tool_names)