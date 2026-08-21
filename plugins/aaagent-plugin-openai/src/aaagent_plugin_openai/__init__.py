from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from aaagent.core.plugin import Provider
from aaagent.core.types import ChatResponse, ToolCall

logger = logging.getLogger("aaagent.provider.openai")


class OpenAICompatibleProvider(Provider):
    """OpenAI-compatible LLM provider.

    Works against any OpenAI-style Chat Completions endpoint (OpenAI,
    DeepSeek, Qwen, MiniMax, cntoken, etc.) by configuring base_url.
    Supports streaming via stream_chat for the no-tools path.
    """

    type = "openai_compatible"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        api_key = config.get("api_key", "")
        if isinstance(api_key, str) and api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")

        if not api_key:
            logger.error(
                "Provider '%s': api_key missing (set env var referenced in config)",
                config.get("_name", "openai_compatible"),
            )

        self._api_key = api_key or None
        self._base_url = config.get("base_url") or None
        self._model = config.get("model", "gpt-4o")
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = self._get_client()
        kwargs.pop("tools", None)
        response = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            **kwargs,
        )
        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] | None = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                for tc in msg.tool_calls
            ]

        return ChatResponse(content=msg.content or "", tool_calls=tool_calls)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        if kwargs.get("tools"):
            raise NotImplementedError(
                "stream_chat does not support tool calls; use chat() instead"
            )
        kwargs.pop("tools", None)
        client = self._get_client()
        stream = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


__all__ = ["OpenAICompatibleProvider"]