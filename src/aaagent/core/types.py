from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] | None = None


class LLMProvider(ABC):
    """Legacy LLM provider ABC retained for backward compatibility.

    New plugins should subclass ``aaagent.core.plugin.Provider`` instead.
    This class is still used by ``Application`` for the internal provider
    shim and by tests via ``FakeProvider``.
    """

    name: str = ""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse: ...

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        raise NotImplementedError(
            f"{type(self).__name__} does not support stream_chat"
        )
        yield ""  # pragma: no cover


PROVIDER_TYPE_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider_type(provider_type: str) -> type:
    def decorator(cls: type[LLMProvider]) -> type[LLMProvider]:
        PROVIDER_TYPE_REGISTRY[provider_type] = cls
        return cls
    return decorator