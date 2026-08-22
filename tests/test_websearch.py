from __future__ import annotations

import json

import httpx
import pytest

from aaagent_plugin_websearch import WebSearchToolsPlugin
from aaagent_plugin_websearch.backends.tavily import TavilyBackend


def _payload(query: str = "test", days: int | None = None) -> dict:
    body = {
        "api_key": "test-key",
        "query": query,
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
    }
    if days is not None:
        body["days"] = days
    return body


def _tavily_response(results: list[dict]) -> dict:
    return {"query": "test", "results": results, "answer": None}


def _make_backend(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return TavilyBackend(api_key="test-key", client=client), client


@pytest.mark.asyncio
async def test_tavily_backend_parses_results():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_tavily_response(
                [
                    {
                        "title": "Example",
                        "url": "https://example.com/",
                        "content": "Hello world.",
                    },
                    {
                        "title": "Second",
                        "url": "https://example.com/2",
                        "content": "Another snippet.",
                    },
                ]
            ),
        )

    backend, client = _make_backend(handler)
    try:
        results = await backend.search(
            "hello",
            top_k=2,
            recency_days=None,
            timeout=5.0,
        )
    finally:
        await client.aclose()

    assert captured["url"].endswith("/search")
    assert captured["body"]["query"] == "hello"
    assert captured["body"]["max_results"] == 2
    assert "days" not in captured["body"]
    assert len(results) == 2
    assert results[0].title == "Example"
    assert results[0].url == "https://example.com/"
    assert "Hello world" in results[0].snippet


@pytest.mark.asyncio
async def test_tavily_backend_passes_recency_days():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_tavily_response([]))

    backend, client = _make_backend(handler)
    try:
        await backend.search("x", top_k=3, recency_days=7, timeout=5.0)
    finally:
        await client.aclose()

    assert captured["body"]["days"] == 7


@pytest.mark.asyncio
async def test_tavily_backend_handles_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    backend, client = _make_backend(handler)
    try:
        with pytest.raises(RuntimeError, match="401"):
            await backend.search("x", top_k=3, recency_days=None, timeout=5.0)
    finally:
        await client.aclose()


def test_tavily_backend_requires_api_key():
    with pytest.raises(ValueError, match="API key is required"):
        TavilyBackend(api_key="")


@pytest.mark.asyncio
async def test_web_search_tool_formats_results():
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"websearch": {"enabled": True, "backend": "tavily", "api_key": "k"}}},
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json=_tavily_response(
                [
                    {
                        "title": "Foo",
                        "url": "https://foo.example/",
                        "content": "Foo content.",
                    }
                ]
            ),
        )

    plugin._backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    try:
        out = await plugin._handler({"query": "foo", "top_k": 1})
    finally:
        await plugin.close()

    assert captured["body"]["query"] == "foo"
    assert "[1] Foo" in out
    assert "https://foo.example/" in out
    assert "Foo content." in out


@pytest.mark.asyncio
async def test_web_search_tool_requires_query():
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"websearch": {"enabled": True, "backend": "tavily", "api_key": "k"}}},
    )
    try:
        out = await plugin._handler({"query": ""})
    finally:
        await plugin.close()
    assert "Error" in out and "query" in out


@pytest.mark.asyncio
async def test_web_search_tool_disabled_returns_nothing():
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"websearch": {"enabled": False}}},
    )
    assert plugin._backend is None
    out = await plugin._handler({"query": "x"})
    assert "not initialized" in out


@pytest.mark.asyncio
async def test_web_search_tool_backend_error_is_caught():
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {"tools": {"websearch": {"enabled": True, "backend": "tavily", "api_key": "k"}}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    plugin._backend._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        out = await plugin._handler({"query": "x"})
    finally:
        await plugin.close()
    assert "Error" in out and "500" in out


def test_register_resolves_env_placeholder(monkeypatch):
    monkeypatch.setenv("TAVILY_TEST_KEY", "sk-real-12345")
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {
            "tools": {
                "websearch": {
                    "enabled": True,
                    "backend": "tavily",
                    "api_key": "${TAVILY_TEST_KEY}",
                }
            }
        },
    )
    assert plugin._backend._api_key == "sk-real-12345"


def test_register_strips_whitespace():
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    plugin.register(
        registry,
        {
            "tools": {
                "websearch": {
                    "enabled": True,
                    "backend": "tavily",
                    "api_key": "  tvly-abc  ",
                }
            }
        },
    )
    assert plugin._backend._api_key == "tvly-abc"


def test_register_raises_on_missing_key(monkeypatch):
    monkeypatch.delenv("TAVILY_DEFINITELY_NOT_SET", raising=False)
    plugin = WebSearchToolsPlugin()
    registry = type("R", (), {"register": lambda self, **kw: None})()
    with pytest.raises(RuntimeError, match="api_key missing"):
        plugin.register(
            registry,
            {
                "tools": {
                    "websearch": {
                        "enabled": True,
                        "backend": "tavily",
                        "api_key": "${TAVILY_DEFINITELY_NOT_SET}",
                    }
                }
            },
        )