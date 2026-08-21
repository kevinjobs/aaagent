"""Builtin plugin wrappers for in-tree provider/adapter/tool implementations.

These wrappers adapt the existing concrete classes (which use the legacy
``LLMProvider`` / ``IMAdapter`` ABCs) to the new ``Provider`` / ``IMAdapter``
plugin protocols. They will be moved to the corresponding
``aaagent-plugin-*`` packages in commits 5–7.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from aaagent.core.plugin import IMAdapter, Provider, ToolPlugin

logger = logging.getLogger("aaagent.builtin_wrappers")


class _BuiltinOpenAIProvider(Provider):
    type = "openai_compatible"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        from aaagent.providers.openai import OpenAICompatibleProvider

        self._inner = OpenAICompatibleProvider(
            name=config.get("_name", "openai_compatible"),
            config=config,
        )

    async def chat(self, messages, tools=None, **kwargs):
        return await self._inner.chat(messages, tools=tools, **kwargs)

    async def stream_chat(
        self, messages, **kwargs
    ) -> AsyncIterator[str]:
        async for chunk in self._inner.stream_chat(messages, **kwargs):
            yield chunk


class _BuiltinCliAdapter(IMAdapter):
    name = "cli"

    def __init__(self, config: dict[str, Any], bus: Any) -> None:
        super().__init__(config, bus)
        from aaagent.adapters.cli_adapter import CliAdapter

        self._inner = CliAdapter(config, bus)

    async def start(self) -> None:
        return await self._inner.start()

    async def stop(self) -> None:
        return await self._inner.stop()

    async def send(self, msg) -> None:
        return await self._inner.send(msg)

    async def health_check(self) -> bool:
        return await self._inner.health_check()


class _BuiltinFeishuAdapter(IMAdapter):
    name = "feishu"

    def __init__(self, config: dict[str, Any], bus: Any) -> None:
        super().__init__(config, bus)
        from aaagent.adapters.feishu import FeishuAdapter

        self._inner = FeishuAdapter(config, bus)

    async def start(self) -> None:
        return await self._inner.start()

    async def stop(self) -> None:
        return await self._inner.stop()

    async def send(self, msg) -> None:
        return await self._inner.send(msg)

    async def health_check(self) -> bool:
        return await self._inner.health_check()


class _BuiltinWechatAdapter(IMAdapter):
    name = "wechat"

    def __init__(self, config: dict[str, Any], bus: Any) -> None:
        super().__init__(config, bus)
        from aaagent.adapters.wechat import WechatAdapter

        self._inner = WechatAdapter(config, bus)

    async def start(self) -> None:
        return await self._inner.start()

    async def stop(self) -> None:
        return await self._inner.stop()

    async def send(self, msg) -> None:
        return await self._inner.send(msg)

    async def health_check(self) -> bool:
        return await self._inner.health_check()


class _BuiltinFileTools(ToolPlugin):
    name = "file"

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        from aaagent.tools.file_tools import register_file_tools

        register_file_tools(registry)


class _BuiltinShellTools(ToolPlugin):
    name = "shell"

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        from aaagent.tools.shell_tools import register_shell_tools

        shell_cfg = config.get("shell", {}) if isinstance(config, dict) else {}
        if shell_cfg.get("enabled", True):
            register_shell_tools(registry)


class _BuiltinMemoryTools(ToolPlugin):
    name = "memory"
    uses_memory_store = True

    def __init__(self) -> None:
        self._memory_store: Any = None

    def set_memory(self, store: Any) -> None:
        self._memory_store = store

    def register(self, registry: Any, config: dict[str, Any]) -> None:
        from aaagent.tools.memory_tools import register_memory_tools

        memory_cfg = config.get("memory", {}) if isinstance(config, dict) else {}
        if not memory_cfg.get("enabled", True):
            return
        register_memory_tools(registry, memory_store=self._memory_store)


BUILTIN_PROVIDERS: dict[str, str] = {
    "openai_compatible": "aaagent.core._builtin_wrappers:_BuiltinOpenAIProvider",
}

BUILTIN_ADAPTERS: dict[str, str] = {
    "cli": "aaagent.core._builtin_wrappers:_BuiltinCliAdapter",
    "feishu": "aaagent.core._builtin_wrappers:_BuiltinFeishuAdapter",
    "wechat": "aaagent.core._builtin_wrappers:_BuiltinWechatAdapter",
}

BUILTIN_TOOLS: dict[str, str] = {
    "file": "aaagent.core._builtin_wrappers:_BuiltinFileTools",
    "shell": "aaagent.core._builtin_wrappers:_BuiltinShellTools",
    "memory": "aaagent.core._builtin_wrappers:_BuiltinMemoryTools",
}