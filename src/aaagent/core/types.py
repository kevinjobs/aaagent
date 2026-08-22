from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from aaagent.core.plugin import Provider


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] | None = None


class LLMProvider(Provider):
    """Backward-compatible alias for ``Provider``.

    The core accepts both shapes via duck typing; the legacy
    ``LLMProvider`` class is preserved so existing tests and ``FakeProvider``
    subclasses keep working without changes. New plugins should subclass
    ``aaagent.core.plugin.Provider`` (or this class) directly.
    """

    name: str = ""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.name = name

    async def chat(  # type: ignore[override]
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        raise NotImplementedError

    async def stream_chat(  # type: ignore[override]
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        raise NotImplementedError(
            f"{type(self).__name__} does not support stream_chat"
        )
        yield ""  # pragma: no cover


PROVIDER_TYPE_REGISTRY: dict[str, type[Provider]] = {}


def register_provider_type(provider_type: str) -> type:
    def decorator(cls: type[Provider]) -> type[Provider]:
        PROVIDER_TYPE_REGISTRY[provider_type] = cls
        return cls
    return decorator