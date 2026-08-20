from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI

from aaagent.providers.base import LLMProvider, register_provider_type


@register_provider_type("openai_compatible")
class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, name: str, config: dict[str, Any]) -> None:
        super().__init__(name, config)
        api_key = config.get("api_key", "")
        if api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")

        self._api_key = api_key or None
        self._base_url = config.get("base_url") or None
        self._model = config.get("model", "gpt-4o")
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            **kwargs,
        )
        return response.choices[0].message.content or ""
