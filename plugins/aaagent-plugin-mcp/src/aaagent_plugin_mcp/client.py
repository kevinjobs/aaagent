"""Lazy MCP client session management for aaagent.

A single `McpServerSession` wraps one MCP server (stdio or streamable HTTP)
and exposes tool discovery / invocation. Connections are established lazily
on first use and re-used until `close()`.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("aaagent.mcp")


def _serialize_mcp_result(result: types.CallToolResult) -> str:
    """Flatten an MCP CallToolResult into a plain string for the LLM."""
    if not result.content:
        return f"[isError: {result.isError}]" if result.isError else ""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            parts.append(f"[image: {getattr(block, 'mimeType', 'image')}]")
        else:
            dump = getattr(block, "model_dump", None)
            parts.append(str(dump() if callable(dump) else block))
    return "\n".join(parts)


def _clean_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Strip JSON-schema keys the OpenAI-compatible tool layer dislikes."""
    cleaned = dict(schema or {})
    cleaned.pop("$schema", None)
    cleaned.pop("title", None)
    return cleaned


class McpServerSession:
    """A lazily-connected session to one MCP server."""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self._lock = asyncio.Lock()
        self._cm = None
        self._session: ClientSession | None = None

    def _transport_params(self) -> Any:
        transport = self.config.get("transport", "stdio")
        if transport == "http":
            url = self.config.get("url", "")
            if not url:
                raise ValueError(f"MCP server '{self.name}': http transport needs a 'url'")
            return url
        if transport == "stdio":
            raw = self.config.get("command")
            if not raw:
                raise ValueError(f"MCP server '{self.name}': stdio transport needs a 'command'")
            if isinstance(raw, str):
                parts = shlex.split(raw)
            else:
                parts = [str(p) for p in raw]
            command = parts[0]
            args = parts[1:]
            env = None
            cfg_env = self.config.get("env")
            if cfg_env and isinstance(cfg_env, dict):
                import os

                env = dict(os.environ)
                env.update({k: str(v) for k, v in cfg_env.items()})
            return StdioServerParameters(command=command, args=args, env=env)
        raise ValueError(
            f"MCP server '{self.name}': unsupported transport '{transport}' "
            "(expected 'stdio' or 'http')"
        )

    async def _ensure_connected(self) -> ClientSession:
        session = self._session
        if session is not None:
            return session
        async with self._lock:
            if self._session is not None:
                return self._session
            params = self._transport_params()
            if isinstance(params, StdioServerParameters):
                cm = stdio_client(params)
            else:
                cm = streamable_http_client(params)
            try:
                read, write = await cm.__aenter__()
                self._cm = cm
            except Exception:
                logger.exception("MCP server '%s': failed to open transport", self.name)
                raise
            session = ClientSession(read, write)
            try:
                await session.__aenter__()
                await session.initialize()
            except Exception:
                await session.__aexit__(None, None, None)
                if self._cm is not None:
                    try:
                        await self._cm.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001
                        pass
                    self._cm = None
                raise
            self._session = session
            logger.info("Connected to MCP server '%s'", self.name)
            return session

    async def list_tools(self) -> list[types.Tool]:
        session = await self._ensure_connected()
        result = await session.list_tools()
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        session = await self._ensure_connected()
        result = await session.call_tool(tool_name, arguments or {})
        return _serialize_mcp_result(result)

    async def close(self) -> None:
        session, self._session = self._session, None
        cm, self._cm = self._cm, None
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("MCP server '%s': session close failed", self.name)
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("MCP server '%s': transport close failed", self.name)
        logger.info("Closed MCP server '%s'", self.name)