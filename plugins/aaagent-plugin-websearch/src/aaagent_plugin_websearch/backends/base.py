"""Backend abstraction for web search providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class Backend(ABC):
    """Abstract search backend.

    Backends translate a query into a list of SearchResult. Implementations
    should be safe to construct once and call repeatedly.
    """

    name: str

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        top_k: int,
        recency_days: int | None,
        timeout: float,
    ) -> list[SearchResult]:
        ...

    @abstractmethod
    async def aclose(self) -> None:
        ...