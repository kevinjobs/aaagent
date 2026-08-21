"""MCP tool plugin for aaagent.

Configures one or more MCP servers from the top-level `mcp` config section:

```yaml
mcp:
  servers:
    - name: fs                       # tool-name prefix
      transport: stdio               # stdio | http
      command: [...npx -y @modelcontextprotocol/server-filesystem .]
    - name: remote
      transport: http
      url: "http://localhost:8080/mcp"
```

Each server's tools are auto-expanded into the ToolRegistry under the
`{server_name}_{tool_name}` namespace. Connections are opened lazily by the
app's tool-plugin `establish` hook and torn down on `close`.
"""

from __future__ import annotations

import logging
from typing import Any

from aaagent.core.plugin import ToolPlugin
from aaagent.core.tool_registry import ToolRegistry

from aaagent_plugin_mcp.client import McpServerSession, _clean_schema

logger = logging.getLogger("aaagent.mcp")


class McpToolsPlugin(ToolPlugin):
    name = "mcp"

    def __init__(self) -> None:
        self._sessions: list[McpServerSession] = []

    def register(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        cfg = config.get("mcp", {}) or {}
        for server in cfg.get("servers", []) or []:
            if not isinstance(server, dict):
                continue
            if not server.get("enabled", True):
                continue
            name = server.get("name") or server.get("server")
            if not name:
                logger.warning("Skipping MCP server config without a name: %s", server)
                continue
            self._sessions.append(McpServerSession(str(name), server))
        if self._sessions:
            logger.info("Configured %d MCP server(s)", len(self._sessions))

    async def establish(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        for session in self._sessions:
            try:
                tools = await session.list_tools()
            except Exception as e:  # noqa: BLE001
                logger.error("MCP server '%s': discovery failed, skipping: %s", session.name, e)
                continue
            for tool in tools:
                registry.register(
                    name=f"{session.name}_{tool.name}",
                    description=tool.description or f"MCP tool {tool.name}",
                    parameters=_clean_schema(tool.inputSchema),
                    handler=self._make_handler(session, tool.name),
                )
            logger.info(
                "MCP server '%s': expanded %d tool(s)", session.name, len(tools)
            )

    def _make_handler(self, session: McpServerSession, tool_name: str):
        async def handler(args: dict[str, Any]) -> str:
            return await session.call_tool(tool_name, args)

        return handler

    async def close(self) -> None:
        for session in self._sessions:
            await session.close()
        self._sessions.clear()


__all__ = ["McpToolsPlugin"]