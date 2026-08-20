from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from aaagent.adapters.base import IMAdapter
from aaagent.adapters.cli_adapter import CliAdapter
from aaagent.adapters.feishu import FeishuAdapter
from aaagent.adapters.wechat import WechatAdapter
from aaagent.core.bus import EventBus
from aaagent.core.message import Message
from aaagent.core.session import SessionStore
from aaagent.providers.base import LLMProvider, PROVIDER_TYPE_REGISTRY
from aaagent.providers.openai import OpenAICompatibleProvider
from aaagent.tools.file_tools import register_file_tools
from aaagent.tools.registry import ToolRegistry
from aaagent.tools.shell_tools import register_shell_tools

logger = logging.getLogger("aaagent")

_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
_PUBLIC_ERROR = "服务暂时不可用，请稍后再试。"
_MAX_TOOL_TURNS = 20


def _strip_think(text: str) -> str:
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


ADAPTER_REGISTRY: dict[str, type[IMAdapter]] = {
    "cli": CliAdapter,
    "feishu": FeishuAdapter,
    "wechat": WechatAdapter,
}


def _resolve_provider(name: str, cfg: dict[str, Any]) -> LLMProvider | None:
    provider_type = cfg.get("type", "")

    if provider_type == "custom":
        class_path = cfg.get("class", "")
        if not class_path:
            logger.error("Custom provider '%s' missing 'class' field", name)
            return None
        module_path, _, cls_name = class_path.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, cls_name)
        except (ImportError, AttributeError) as e:
            logger.error("Failed to load custom provider '%s': %s", name, e)
            return None
        return cls(name=name, config=cfg)

    provider_cls = PROVIDER_TYPE_REGISTRY.get(provider_type)
    if provider_cls is None:
        logger.error("Unknown provider type '%s' for provider '%s'", provider_type, name)
        return None
    return provider_cls(name=name, config=cfg)


class Application:
    def __init__(self, config_path: str = "config.yaml") -> None:
        from dotenv import load_dotenv
        load_dotenv()
        self._config = self._load_config(config_path)
        self._bus = EventBus()
        self._session_store = SessionStore(
            max_history=self._config.get("session", {}).get("max_history", 20),
            compress_threshold=self._config.get("session", {}).get("compress_threshold", 0.8),
        )
        self._adapters: list[IMAdapter] = []
        self._providers: dict[str, LLMProvider] = {}
        self._provider: LLMProvider | None = None
        self._tool_registry = self._setup_tool_registry()
        self._setup()

    def _load_config(self, path: str) -> dict[str, Any]:
        p = Path(path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _setup_tool_registry(self) -> ToolRegistry:
        tools_cfg = self._config.get("tools", {})
        allowed_dirs = tools_cfg.get("allowed_dirs", None)
        if allowed_dirs is None:
            allowed_dirs = [str(Path.cwd())]
        else:
            allowed_dirs = [str(Path(d).resolve()) for d in allowed_dirs]

        registry = ToolRegistry(allowed_dirs=allowed_dirs)
        register_file_tools(registry)
        if tools_cfg.get("shell", {}).get("enabled", True):
            register_shell_tools(registry)
        logger.info(
            "Tool registry initialized with %d tools, allowed_dirs=%s",
            len(registry.tool_names),
            allowed_dirs,
        )
        return registry

    def _setup(self) -> None:
        self._setup_providers()
        self._setup_adapters()
        self._setup_event_handlers()

    def _setup_providers(self) -> None:
        providers_cfg = self._config.get("providers", {})
        default_name = self._config.get("default_provider", "")

        for name, cfg in providers_cfg.items():
            if not cfg.get("enabled", False):
                continue
            provider = _resolve_provider(name, cfg)
            if provider is None:
                continue
            self._providers[name] = provider
            logger.info("Loaded provider: %s (type=%s)", name, cfg.get("type", ""))

        if default_name and default_name in self._providers:
            self._provider = self._providers[default_name]
        elif self._providers:
            self._provider = next(iter(self._providers.values()))
            logger.info("No default_provider set, using first provider: %s", self._provider.name)

    def _setup_adapters(self) -> None:
        adapters_cfg = self._config.get("adapters", {})
        for name, cfg in adapters_cfg.items():
            if not cfg.get("enabled", False):
                continue
            cls = ADAPTER_REGISTRY.get(name)
            if cls is None:
                logger.warning("Unknown adapter: %s", name)
                continue
            adapter = cls(cfg, self._bus)
            self._adapters.append(adapter)
            logger.info("Loaded adapter: %s", name)

    def _setup_event_handlers(self) -> None:
        self._bus.on("message_received", self._on_message_received)

    async def _on_message_received(self, msg: Message) -> None:
        if self._provider is None:
            logger.error("No LLM provider configured")
            return

        await self._session_store.add_message(msg.session_id, msg)

        context = await self._session_store.get_context(msg.session_id)

        try:
            reply_text = await self._run_tool_loop(
                msg.session_id, msg.platform, msg.chat_id, context
            )
            reply_text = _strip_think(reply_text)
        except Exception as e:
            logger.error("LLM call failed for session %s: %s", msg.session_id, e, exc_info=True)
            reply_text = _PUBLIC_ERROR

        reply_msg = Message(
            session_id=msg.session_id,
            platform=msg.platform,
            chat_id=msg.chat_id,
            user_id="assistant",
            content=reply_text,
            role="assistant",
        )

        await self._session_store.add_message(msg.session_id, reply_msg)
        await self._session_store.maybe_compress(msg.session_id, self._provider)

        await self._bus.emit("message_to_send", reply_msg)

    async def _run_tool_loop(
        self,
        session_id: str,
        platform: str,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        tools = self._tool_registry.definitions
        if not tools:
            result = await self._provider.chat(messages)
            return result.content

        for turn in range(1, _MAX_TOOL_TURNS + 1):
            result = await self._provider.chat(messages, tools=tools)

            if not result.tool_calls:
                return result.content

            tool_role_msg: dict[str, Any] = {"role": "assistant", "content": result.content or None}
            tool_calls_dicts: list[dict[str, Any]] = []
            for tc in result.tool_calls:
                tool_calls_dicts.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                })
            tool_role_msg["tool_calls"] = tool_calls_dicts
            messages.append(tool_role_msg)

            await self._bus.emit(
                "tool_start",
                {
                    "session_id": session_id,
                    "platform": platform,
                    "chat_id": chat_id,
                    "tool_calls": result.tool_calls,
                    "turn": turn,
                },
            )

            for tc in result.tool_calls:
                output = await self._tool_registry.execute(tc.name, tc.arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output,
                })

                await self._bus.emit(
                    "tool_result",
                    {
                        "session_id": session_id,
                        "platform": platform,
                        "chat_id": chat_id,
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "arguments": tc.arguments,
                        "result": output,
                        "turn": turn,
                    },
                )

            msg = Message(
                session_id=session_id,
                platform=platform,
                chat_id=chat_id,
                user_id="assistant",
                content=result.content,
                role="assistant",
                tool_calls=tool_calls_dicts,
            )
            await self._session_store.add_message(session_id, msg)

            for tc in result.tool_calls:
                tool_msg = Message(
                    session_id=session_id,
                    platform=platform,
                    chat_id=chat_id,
                    user_id="system",
                    content=tc.arguments,
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                )
                await self._session_store.add_message(session_id, tool_msg)

        logger.warning("Tool loop exceeded max turns (%d) for session %s", _MAX_TOOL_TURNS, session_id)
        return "已达到最大工具调用次数。"

    def add_adapter(self, adapter: IMAdapter) -> None:
        self._adapters.append(adapter)

    def set_provider(self, provider: LLMProvider) -> None:
        self._provider = provider

    def get_provider(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

    @property
    def provider(self) -> LLMProvider | None:
        return self._provider

    async def run(self) -> None:
        if not self._adapters:
            logger.error("No adapters configured, nothing to run")
            return

        try:
            tasks = [adapter.start() for adapter in self._adapters]
            await asyncio.gather(*tasks)
        finally:
            await self.stop()

    async def stop(self) -> None:
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("Error stopping adapter %s: %s", type(adapter).__name__, e)
