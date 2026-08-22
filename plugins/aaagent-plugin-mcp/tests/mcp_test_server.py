"""FastMCP-based stdio server used by the aaagent MCP plugin tests."""

from __future__ import annotations

from fastmcp import FastMCP

server = FastMCP("aaagent-test-server")


@server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers together."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Echo back the input text unchanged."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")