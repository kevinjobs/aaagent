"""Web search tool plugin for aaagent (Tavily backend by default).

Config example (config.yaml):

    tools:
      websearch:
        enabled: true
        backend: tavily
        api_key: "${TAVILY_API_KEY}"
        max_results: 5
        timeout: 15
        include_answer: true
"""

from __future__ import annotations

import logging
from typing import Any

from aaagent.core.envutil import resolve_env
from aaagent.core.plugin import ToolPlugin

from .backends.base import Backend, SearchResult
from .backends.tavily import TavilyBackend

logger = logging.getLogger("aaagent.tools.websearch")

_BACKENDS: dict[str, type[Backend]] = {
    "tavily": TavilyBackend,
}


def _format_results(
    results: list[SearchResult], *, include_answer: bool, answer: str | None
) -> str:
    parts: list[str] = []
    if include_answer and answer:
        parts.append(f"[answer]\n{answer}\n")
    if not results:
        parts.append("(no results)")
        return "\n".join(parts)
    for i, r in enumerate(results, 1):
        title = r.title or "(no title)"
        snippet = r.snippet.replace("\n", " ").strip()
        parts.append(f"[{i}] {title} — {r.url}\n{snippet}")
    return "\n\n".join(parts)


async def web_search(
    args: dict[str, Any], *, backend: Backend, max_results: int, timeout: float
) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required"

    top_k = args.get("top_k")
    if top_k is None:
        top_k = max_results
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = max_results
    top_k = max(1, min(top_k, 20))

    recency_days: int | None = None
    rd = args.get("recency_days")
    if rd is not None:
        try:
            recency_days = int(rd)
            if recency_days <= 0:
                recency_days = None
        except (TypeError, ValueError):
            recency_days = None

    include_answer = bool(args.get("include_answer", False))

    try:
        results = await backend.search(
            query,
            top_k=top_k,
            recency_days=recency_days,
            timeout=timeout,
        )
    except TimeoutError as e:
        return f"Error: web search timed out ({e})"
    except RuntimeError as e:
        logger.warning("web_search backend error: %s", e)
        return f"Error: web search failed ({e})"
    except Exception as e:  # noqa: BLE001
        logger.exception("web_search unexpected error")
        return f"Error: web search failed ({e})"

    return _format_results(results, include_answer=include_answer, answer=None)


class WebSearchToolsPlugin(ToolPlugin):
    name = "websearch"

    def __init__(self) -> None:
        self._backend: Backend | None = None
        self._max_results: int = 5
        self._timeout: float = 15.0

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        cfg = (config.get("tools") or {}).get("websearch") or {}
        if not cfg.get("enabled", True):
            return

        backend_name = (cfg.get("backend") or "tavily").lower()
        backend_cls = _BACKENDS.get(backend_name)
        if backend_cls is None:
            raise RuntimeError(
                f"Unknown websearch backend '{backend_name}'. "
                f"Available: {sorted(_BACKENDS)}"
            )

        api_key = resolve_env(cfg.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(
                "websearch api_key missing (set the env var referenced in "
                "tools.websearch.api_key, e.g. TAVILY_API_KEY in .env)"
            )
        try:
            self._backend = backend_cls(api_key=api_key)
        except ValueError as e:
            raise RuntimeError(f"websearch backend init failed: {e}") from e

        self._max_results = int(cfg.get("max_results", 5))
        self._timeout = float(cfg.get("timeout", 15))

        registry.register(
            name="web_search",
            description=(
                "Search the public web. Returns a numbered list of {title, url, snippet}. "
                "Use when you need up-to-date information, references, or to discover "
                "URLs to fetch in detail. Requires a configured backend (e.g. Tavily API key)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, in natural language.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (1-20, default 5).",
                    },
                    "recency_days": {
                        "type": "integer",
                        "description": "Optional: only include results from the last N days.",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "description": "Optional: include an AI-generated answer summary (backend-dependent).",
                    },
                },
                "required": ["query"],
            },
            handler=self._handler,
        )
        logger.info("websearch plugin registered (backend=%s)", backend_name)

    async def establish(self, registry: Any, config: dict[str, Any]) -> None:
        return None

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.aclose()
            self._backend = None

    async def _handler(self, args: dict[str, Any]) -> str:
        if self._backend is None:
            return "Error: websearch backend not initialized"
        return await web_search(
            args,
            backend=self._backend,
            max_results=self._max_results,
            timeout=self._timeout,
        )


__all__ = ["WebSearchToolsPlugin", "web_search"]