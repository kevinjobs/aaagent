"""Tavily search backend.

Uses the Tavily Search API: https://docs.tavily.com/docs/rest-api/api-reference
Free tier: 1000 requests/month, returns LLM-friendly snippets plus an optional
AI-generated answer.
"""

from __future__ import annotations

import logging

import httpx

from .base import Backend, SearchResult

logger = logging.getLogger("aaagent.tools.websearch.tavily")

_API_URL = "https://api.tavily.com/search"


class TavilyBackend(Backend):
    name = "tavily"

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("Tavily API key is required")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        recency_days: int | None,
        timeout: float,
    ) -> list[SearchResult]:
        payload: dict = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max(1, min(top_k, 20)),
            "include_answer": False,
            "include_raw_content": False,
        }
        if recency_days is not None and recency_days > 0:
            payload["days"] = recency_days

        try:
            resp = await self._client.post(
                _API_URL,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            raise TimeoutError(f"Tavily request timed out after {timeout}s")
        except httpx.HTTPError as e:
            raise RuntimeError(f"Tavily HTTP error: {e}")

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"Tavily API error {resp.status_code}: {body}")

        data = resp.json()
        results: list[SearchResult] = []
        for item in data.get("results", []) or []:
            results.append(
                SearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=str(item.get("url", "")).strip(),
                    snippet=str(item.get("content", "")).strip(),
                )
            )
        return results

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()