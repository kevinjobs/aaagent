from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
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
        """Yield text chunks for a streaming reply.

        Default raises NotImplementedError; subclasses with native
        streaming support should override. Streaming with tool calls is
        not supported — fall back to chat() in that case.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support stream_chat"
        )
        yield ""  # pragma: no cover — makes this an async generator


PROVIDER_TYPE_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider_type(provider_type: str) -> type:
    def decorator(cls: type[LLMProvider]) -> type:
        PROVIDER_TYPE_REGISTRY[provider_type] = cls
        return cls
    return decorator
