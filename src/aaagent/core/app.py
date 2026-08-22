from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from aaagent.core._builtin_wrappers import (
    BUILTIN_ADAPTERS,
    BUILTIN_MEMORIES,
    BUILTIN_PROVIDERS,
    BUILTIN_SESSIONS,
    BUILTIN_TOOLS,
)
from aaagent.core.bus import EventBus
from aaagent.core.commands import (
    SlashCommandRegistry,
    SlashContext,
    SlashResult,
    register_builtins,
)
from aaagent.core.logctx import reset_context, set_context
from aaagent.core.memory import MemoryStore
from aaagent.core.message import Message
from aaagent.core.plugin import (
    IMAdapter,
    PluginManager,
    PluginNotFoundError,
    Provider,
)
from aaagent.core.prompt import PromptBuilder
from aaagent.core.ratelimit import TokenBucket
from aaagent.core.session import SessionStore
from aaagent.core.types import ChatResponse, LLMProvider, PROVIDER_TYPE_REGISTRY
from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent")

_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
_PUBLIC_ERROR = "服务暂时不可用，请稍后再试。"
_MAX_TOOL_TURNS = 20
_MAX_TOOL_CHARS = 200_000

_REDACT_PATTERNS = [
    (re.compile(r'(?i)(api_key\s*[:=]\s*)["\']?[A-Za-z0-9._\-]+["\']?'), r"\1***"),
    (re.compile(r'(?i)(app_secret\s*[:=]\s*)["\']?[A-Za-z0-9._\-]+["\']?'), r"\1***"),
    (re.compile(r'(?i)(\btoken\s*[:=]\s*)["\']?[A-Za-z0-9._\-]{6,}["\']?'), r"\1***"),
    (re.compile(r'(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]+'), r"\1***"),
]


def _redact_yaml(yaml_text: str) -> str:
    for pat, repl in _REDACT_PATTERNS:
        yaml_text = pat.sub(repl, yaml_text)
    return yaml_text


def _strip_think(text: str) -> str:
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


_RETRYABLE_EXC_TYPES = {
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "AuthenticationError",  # stale/rotated credentials often heal on retry via fallback
    "InternalServerError",
    "RateLimitError",
    "ServiceUnavailableError",
}

_RETRYABLE_MARKERS = (
    "429",
    " 500",
    " 502",
    " 503",
    " 504",
    "rate limit",
    "rate_limit",
    "overloaded",
    "overloaded_error",
    "temporarily unavailable",
    "try again later",
    "connection reset",
    "connection refused",
    "connection aborted",
    "broken pipe",
    "timed out",
    "timeout error",
    "server disconnected",
    # Moderation / policy blocks are deterministic on a single provider but
    # different providers enforce different policies — falling through the
    # fallback chain is usually the right move (e.g. MiniMax "new_sensitive"
    # is more aggressive than DeepSeek / 9router).
    "sensitive",
    "unprocessable_entity",
    "content_filter",
    "content_policy_violation",
    "policy_violation",
)


def _is_retryable_provider_error(exc: Exception) -> bool:
    """Best-effort classification of transient provider errors.

    Returns True for network / timeout / 429 / 5xx conditions that are worth
    retrying against a fallback provider; False for everything else.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if type(exc).__name__ in _RETRYABLE_EXC_TYPES:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


class Application:
    def __init__(
        self,
        config_path: str = "config.yaml",
        enabled_adapters: list[str] | None = None,
        bus: EventBus | None = None,
        session_store: SessionStore | None = None,
        memory: MemoryStore | None = None,
        tool_registry: ToolRegistry | None = None,
        providers: dict[str, LLMProvider] | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        from dotenv import load_dotenv
        load_dotenv()
        self._config = self._load_config(config_path)
        self._bus = bus if bus is not None else EventBus()

        # Plugin discovery (loads builtin + entry_points + config overrides)
        self._plugins = plugin_manager if plugin_manager is not None else PluginManager(
            self._config
        )
        self._plugins.BUILTIN_PROVIDERS = dict(BUILTIN_PROVIDERS)
        self._plugins.BUILTIN_ADAPTERS = dict(BUILTIN_ADAPTERS)
        self._plugins.BUILTIN_TOOLS = dict(BUILTIN_TOOLS)
        self._plugins.BUILTIN_SESSIONS = dict(BUILTIN_SESSIONS)
        self._plugins.BUILTIN_MEMORIES = dict(BUILTIN_MEMORIES)
        self._plugins.load()

        # Session / Memory via plugin factories (with fallback to direct construction
        # for tests that inject session_store / memory directly)
        if session_store is None:
            session_cfg = self._config.get("session", {})
            session_type = session_cfg.get("type", "inmemory")
            try:
                factory = self._plugins.get_session_factory(session_type)
                session_store = factory.create(session_cfg)
            except PluginNotFoundError as e:
                logger.warning(
                    "No session store plugin for type '%s': %s; falling back to direct construction",
                    session_type,
                    e,
                )
                session_store = SessionStore(
                    max_history=session_cfg.get("max_history", 20),
                    compress_threshold=session_cfg.get("compress_threshold", 0.8),
                    system_prompt=session_cfg.get("system_prompt", ""),
                )
        self._session_store = session_store
        self._adapters: list[IMAdapter] = []
        self._providers: dict[str, LLMProvider] = providers if providers is not None else {}
        self._provider: LLMProvider | None = None
        # Memory via plugin factory (fallback to direct construction when no plugin)
        if memory is None:
            memory_cfg = self._config.get("memory", {})
            memory_type = memory_cfg.get("type", "markdown")
            memory_cfg.setdefault(
                "base_path",
                str(
                    Path(config_path).resolve().parent
                    if Path(config_path).is_absolute()
                    else Path.cwd()
                ),
            )
            try:
                factory = self._plugins.get_memory_factory(memory_type)
                memory = factory.create(memory_cfg)
            except PluginNotFoundError as e:
                logger.warning(
                    "No memory store plugin for type '%s': %s; falling back to direct construction",
                    memory_type,
                    e,
                )
                memory = MemoryStore(
                    data_dir=memory_cfg.get("data_dir", "data/memories"),
                    base_path=Path(memory_cfg["base_path"]),
                )
        self._memory = memory
        _archive_hours = self._config.get("memory", {}).get("archive_after_hours", 24)
        self._archive_interval = float(_archive_hours or 24) * 3600
        self._tool_plugins: list[Any] = []
        self._tool_registry = tool_registry if tool_registry is not None else self._setup_tool_registry()
        self._enabled_adapters = enabled_adapters
        rate_cfg = self._config.get("rate_limit", {})
        self._provider_rpm = int(rate_cfg.get("provider_rpm", 0))
        self._provider_buckets: dict[str, TokenBucket] = {}
        self._commands = SlashCommandRegistry()
        register_builtins(self._commands)
        self._setup()

    def _load_config(self, path: str) -> dict[str, Any]:
        p = Path(path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            cfg = yaml.safe_load(raw) or {}
            logger.debug("Loaded config (redacted):\n%s", _redact_yaml(raw))
            return cfg
        return {}

    def _setup_tool_registry(self) -> ToolRegistry:
        tools_cfg = self._config.get("tools", {})
        allowed_dirs = tools_cfg.get("allowed_dirs", None)
        if allowed_dirs is None:
            allowed_dirs = [str(Path.cwd())]
        else:
            validated: list[str] = []
            for d in allowed_dirs:
                p = Path(d).resolve()
                if not p.exists():
                    logger.warning("allowed_dirs entry does not exist, skipped: %s", d)
                    continue
                validated.append(str(p))
            if not validated:
                logger.warning("No valid allowed_dirs after validation; defaulting to cwd")
                validated = [str(Path.cwd())]
            allowed_dirs = validated

        registry = ToolRegistry(allowed_dirs=allowed_dirs)

        # Instantiate tool plugin classes via PluginManager, then register.
        # Instances are retained so async establish/close hooks can be driven.
        self._tool_plugins: list[Any] = []
        for plugin_cls in self._plugins.get_tool_classes():
            try:
                plugin = plugin_cls()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Failed to instantiate tool plugin %s: %s",
                    plugin_cls.__name__,
                    e,
                )
                continue
            self._tool_plugins.append(plugin)
            try:
                if hasattr(plugin, "set_memory"):
                    plugin.set_memory(self._memory)
                plugin.register(registry, self._config)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Tool plugin %s.register() failed: %s",
                    plugin_cls.__name__,
                    e,
                )

        logger.info(
            "Tool registry initialized with %d tools, allowed_dirs=%s",
            len(registry.tool_names),
            allowed_dirs,
        )
        return registry

    def _setup(self) -> None:
        if not self._providers:
            self._setup_providers()
        self._resolve_provider_chain()
        self._setup_adapters()
        self._setup_event_handlers()

    def _setup_providers(self) -> None:
        providers_cfg = self._config.get("providers", {})
        default_name = self._config.get("default_provider", "")

        for name, cfg in providers_cfg.items():
            if not cfg.get("enabled", False):
                continue
            provider = self._instantiate_provider(name, cfg)
            if provider is None:
                continue
            self._providers[name] = provider
            logger.info("Loaded provider: %s (type=%s)", name, cfg.get("type", ""))

    def _instantiate_provider(
        self, name: str, cfg: dict[str, Any]
    ) -> LLMProvider | None:
        provider_type = cfg.get("type", "")

        # Legacy custom: load class directly via dotted path
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

        # Plugin-resolved provider
        try:
            plugin_cls = self._plugins.get_provider_class(provider_type)
        except PluginNotFoundError:
            logger.error(
                "Unknown provider type '%s' for provider '%s'", provider_type, name
            )
            return None

        plugin_cfg = dict(cfg)
        plugin_cfg["_name"] = name
        instance = plugin_cls(plugin_cfg)

        # Adapt to legacy LLMProvider interface for the rest of the code
        from aaagent.core.types import LLMProvider as _LP

        if isinstance(instance, _LP):
            return instance

        # Wrap a new-Provider plugin in a thin LLMProvider shim
        class _Shim(_LP):
            def __init__(self, name, plugin):
                super().__init__(name=name, config=plugin.config)
                self._plugin = plugin

            async def chat(self, messages, tools=None, **kwargs):
                return await self._plugin.chat(messages, tools=tools, **kwargs)

        return _Shim(name, instance)

    def _resolve_provider_chain(self) -> None:
        """Pick the primary provider and build the fallback order.

        `self._provider` is the primary used for latency-sensitive calls;
        `self._provider_order` is the ordered list consulted by
        `_chat_with_fallback` when the primary fails transiently.
        """
        default_name = self._config.get("default_provider", "")

        if default_name in self._providers:
            self._provider = self._providers[default_name]
        elif self._providers:
            self._provider = next(iter(self._providers.values()))
            logger.info(
                "No default_provider set, using first provider: %s",
                self._provider.name,
            )
        else:
            self._provider = None

        order: list[LLMProvider] = []
        if self._provider is not None:
            order.append(self._provider)
        for name in self._config.get("fallback_providers", []) or []:
            provider = self._providers.get(name)
            if provider is None:
                logger.warning(
                    "fallback_providers entry '%s' is not a loadable provider, skipped",
                    name,
                )
            elif provider not in order:
                order.append(provider)

        if not order and self._providers:
            order.append(next(iter(self._providers.values())))

        self._provider_order = order
        if not self._provider_order:
            logger.warning("No LLM provider available")
        self._init_provider_buckets()

    def _init_provider_buckets(self) -> None:
        self._provider_buckets = {}
        if self._provider_rpm > 0:
            for provider in self._provider_order:
                self._provider_buckets[provider.name] = TokenBucket(
                    rate_per_min=self._provider_rpm
                )

    async def _acquire_provider_bucket(self, provider: LLMProvider) -> None:
        bucket = self._provider_buckets.get(provider.name)
        if bucket is not None:
            await bucket.acquire()

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs
    ) -> "ChatResponse":
        """Fallback-aware chat used by internal callers (session compress, etc.)."""
        return await self._chat_with_fallback(messages, tools=tools, **kwargs)

    async def _chat_with_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> "ChatResponse":
        providers = self._provider_order or (
            [self._provider] if self._provider is not None else []
        )
        if not providers:
            raise RuntimeError("No LLM provider configured")

        last_exc: Exception | None = None
        for index, provider in enumerate(providers):
            try:
                await self._acquire_provider_bucket(provider)
                return await provider.chat(messages, tools=tools, **kwargs)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if index + 1 >= len(providers):
                    break
                if not _is_retryable_provider_error(e):
                    logger.error(
                        "Provider %s failed with non-retryable error: %s",
                        provider.name,
                        e,
                    )
                    raise
                logger.warning(
                    "Provider %s failed (retryable), trying next: %s",
                    provider.name,
                    e,
                )

        assert last_exc is not None
        raise last_exc

    def _setup_adapters(self) -> None:
        adapters_cfg = self._config.get("adapters", {})
        for name, cfg in adapters_cfg.items():
            if not cfg.get("enabled", False):
                continue
            if self._enabled_adapters is not None and name not in self._enabled_adapters:
                continue
            cls = self._plugins.get_adapter_class(name)
            if cls is None:
                logger.warning("Unknown adapter: %s", name)
                continue
            adapter = cls(cfg, self._bus)
            self._adapters.append(adapter)
            logger.info("Loaded adapter: %s", name)

    def _setup_event_handlers(self) -> None:
        self._bus.on("message_received", self._on_message_received)
        self._bus.on("slash_command", self._on_slash_command)

    def _slash_blacklist(self, platform: str) -> set[str]:
        cfg = self._config.get("slash_command_blacklist", {}) or {}
        raw = cfg.get(platform, []) or []
        return {str(x).lower() for x in raw if isinstance(x, str)}

    async def _on_slash_command(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        platform = str(payload.get("platform", ""))
        session_id = str(payload.get("session_id", ""))
        chat_id = str(payload.get("chat_id", ""))
        ctx = SlashContext(
            platform=platform,
            session_id=session_id,
            chat_id=chat_id,
            app=self,
        )
        result: SlashResult = self._commands.handle(
            text, ctx, blacklist=self._slash_blacklist(platform)
        )

        if result.reply:
            await self._bus.emit(
                "slash_reply",
                {
                    "platform": platform,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "reply": result.reply,
                    "suppressed": result.suppressed,
                },
            )

        if result.switch_session:
            await self._bus.emit(
                "slash_session_switch",
                {
                    "platform": platform,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "new_session": result.switch_session,
                },
            )

        if result.stop_adapter:
            await self._bus.emit(
                "slash_quit",
                {"platform": platform, "session_id": session_id},
            )

        if not result.matched:
            await self._bus.emit(
                "slash_unknown",
                {
                    "platform": platform,
                    "session_id": session_id,
                    "chat_id": chat_id,
                    "text": text,
                },
            )

    async def _establish_tool_plugins(self) -> None:
        for plugin in self._tool_plugins:
            try:
                await plugin.establish(self._tool_registry, self._config)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Tool plugin %s.establish() failed: %s", type(plugin).__name__, e
                )

    async def _on_message_received(self, msg: Message) -> None:
        if self._provider is None:
            logger.error("No LLM provider configured")
            return

        tokens = set_context(
            session_id=msg.session_id,
            platform=msg.platform,
            chat_id=msg.chat_id,
        )
        try:
            await self._handle_message(msg)
        finally:
            reset_context(tokens)

    async def _handle_message(self, msg: Message) -> None:
        await self._session_store.add_message(msg.session_id, msg)

        session = await self._session_store.get_session(msg.session_id)
        profile = await self._memory.recall_profile()
        builder = PromptBuilder(system_prompt=self._session_store._system_prompt)
        context = builder.build(session, profile=profile)

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

        await self._session_store.add_message(msg.session_id, reply_msg, provider=self)
        await self._memory.maybe_consolidate_profile(self)

        await self._bus.emit("message_to_send", reply_msg)

    async def _stream_or_chat(self, messages: list[dict[str, Any]]) -> str:
        """Prefer streaming reply if the provider supports it; else one-shot chat.

        Falls back to the next provider on transient errors, but only while no
        tokens have been emitted yet (a mid-stream failure cannot be retried).
        """
        providers = self._provider_order or (
            [self._provider] if self._provider is not None else []
        )
        if not providers:
            raise RuntimeError("No LLM provider configured")

        last_exc: Exception | None = None
        for index, provider in enumerate(providers):
            stream_fn = getattr(provider, "stream_chat", None)
            started = False
            try:
                if stream_fn is not None:
                    try:
                        chunks: list[str] = []
                        async for chunk in stream_fn(messages):
                            started = True
                            chunks.append(chunk)
                            await self._bus.emit("stream_token", chunk)
                        if chunks:
                            return "".join(chunks)
                    except NotImplementedError:
                        pass
                await self._acquire_provider_bucket(provider)
                result = await provider.chat(messages)
                return result.content
            except Exception as e:  # noqa: BLE001
                if started:
                    raise
                last_exc = e
                if index + 1 >= len(providers):
                    break
                if not _is_retryable_provider_error(e):
                    logger.error(
                        "Provider %s failed with non-retryable error: %s",
                        provider.name,
                        e,
                    )
                    raise
                logger.warning(
                    "Provider %s failed (retryable), trying next: %s",
                    provider.name,
                    e,
                )

        assert last_exc is not None
        raise last_exc

    async def _run_tool_loop(
        self,
        session_id: str,
        platform: str,
        chat_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        tools = self._tool_registry.definitions
        if not tools:
            return await self._stream_or_chat(messages)

        for turn in range(1, _MAX_TOOL_TURNS + 1):
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            if total_chars > _MAX_TOOL_CHARS:
                logger.warning(
                    "Tool loop messages exceed %d chars (%d), aborting for session %s",
                    _MAX_TOOL_CHARS,
                    total_chars,
                    session_id,
                )
                return "上下文过长，已中止。请开启新对话。"
            result = await self._chat_with_fallback(messages, tools=tools)

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
                t0 = time.monotonic()
                output = await self._tool_registry.execute(tc.name, tc.arguments)
                duration_ms = int((time.monotonic() - t0) * 1000)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
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
                        "duration_ms": duration_ms,
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
        self._provider_order = [provider]
        self._init_provider_buckets()

    def get_provider(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

    @property
    def provider(self) -> LLMProvider | None:
        return self._provider

    async def run(self) -> None:
        if not self._adapters:
            logger.error("No adapters configured, nothing to run")
            return

        self._health_task = asyncio.create_task(self._health_check_loop())
        if self._memory is not None and self._archive_interval > 0:
            self._archive_task = asyncio.create_task(self._archive_sweep_loop())
        await self._establish_tool_plugins()
        try:
            tasks = [adapter.start() for adapter in self._adapters]
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.stop()

    async def _health_check_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            for adapter in list(self._adapters):
                try:
                    ok = await adapter.health_check()
                    if not ok:
                        logger.warning(
                            "Adapter %s health check failed",
                            type(adapter).__name__,
                        )
                except Exception:
                    logger.exception(
                        "Adapter %s health_check raised",
                        type(adapter).__name__,
                    )

    def _cancel_background_tasks(self) -> None:
        for attr in ("_health_task", "_archive_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()

    async def _archive_sweep_loop(self) -> None:
        """Periodically archive idle sessions into long-term memory.

        Sessions idle longer than `archive_after_hours` are archived via
        `MemoryStore.archive_session` and then dropped from the session store.
        """
        while True:
            try:
                await asyncio.sleep(self._archive_interval)
                await self._archive_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("Session archive sweep failed")

    async def _archive_idle_sessions(self) -> None:
        memory = self._memory
        store = self._session_store
        if memory is None or store is None:
            return
        cutoff = time.time() - self._archive_interval

        stale: list[tuple[str, float, float, str]] = []
        for session in store.list_sessions():
            if session.last_activity > cutoff:
                continue
            if not session.messages and not session.summary:
                continue
            start = getattr(session, "created_at", session.last_activity)
            stale.append(
                (session.id, session.last_activity, start, session.summary or "")
            )

        if not stale:
            return

        for session_id, end_time, start_time, summary in stale:
            try:
                await memory.archive_session(
                    session_id,
                    summary,
                    start_time,
                    end_time,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to archive session %s", session_id)
                continue
            await store.drop_session(session_id)

    async def stop(self) -> None:
        self._cancel_background_tasks()
        await self._memory.close()
        for plugin in getattr(self, "_tool_plugins", []):
            closer = getattr(plugin, "close", None)
            if closer is None:
                continue
            try:
                if asyncio.iscoroutinefunction(closer):
                    await closer()
                else:
                    closer()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Error closing tool plugin %s: %s", type(plugin).__name__, e
                )
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("Error stopping adapter %s: %s", type(adapter).__name__, e)
