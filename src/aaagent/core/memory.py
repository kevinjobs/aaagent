from __future__ import annotations

import abc
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aaagent.core.types import LLMProvider


logger = logging.getLogger("aaagent.memory")


class MemoryStore(abc.ABC):
    """Abstract base for memory stores.

    Implementations live in aaagent-plugin-* packages and are registered
    as plugins via entry_points "aaagent.memories".
    """

    @abc.abstractmethod
    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        session_id: str = "",
    ) -> str: ...

    @abc.abstractmethod
    async def recall(self, query: str, top_k: int = 10) -> str: ...

    @abc.abstractmethod
    async def recall_profile(self) -> str: ...

    @abc.abstractmethod
    async def archive_session(
        self, session_id: str, summary: str, start_time: float, end_time: float
    ) -> None: ...

    @abc.abstractmethod
    async def maybe_consolidate_profile(
        self, provider: "LLMProvider", threshold: int = 15
    ) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...