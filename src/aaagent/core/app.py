from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aaagent.core.bus import EventBus
from aaagent.core.commands import (
    SlashCommandRegistry,
    SlashContext,
    SlashResult,
    register_builtins,
)
from aaagent.core.config_io import ConfigStore
from aaagent.core.dotenv_io import DotenvStore
from aaagent.core.logctx import reset_context, set_context
from aaagent.core.memory import MemoryStore
from aaagent.core.message import Message
from aaagent.core.paths import resolve_all_paths, resolve_project_path
from aaagent.core.plugin import (
    IMAdapter,
    PluginContext,
    PluginManager,
    PluginNotFoundError,
    Provider,
)
from aaagent.core.prompt import PromptBuilder
from aaagent.core.ratelimit import TokenBucket
from aaagent.core.sanitize import scrub, wrap_existing_handlers
from aaagent.core.session import SessionStore
from aaagent.core.types import ChatResponse, PROVIDER_TYPE_REGISTRY
from aaagent.core.plugin import Provider as _ProviderProtocol
from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent")

_DEFAULT_TOOL_WALLCLOCK_S = 120
_DEFAULT_PROVIDER_RPM = 30
_MAX_TOOL_TURNS = 10  # default for Limits when config doesn't pin it
_MAX_TOOL_CHARS = 200_000


@dataclass
class Limits:
    """Resource caps for the agent's main loop.

    All fields are read from `config.limits.*` at startup. Operators
    who want to bump (or relax) any cap edit `config.yaml` — no code
    change required.

    `max_tool_wallclock_s` is a NEW guard that bounds the total time
    a single `_handle_message` is allowed to spend inside
    `_run_tool_loop`. Without it a runaway LLM could keep the loop
    alive indefinitely as long as it produces tool calls within the
    per-turn iteration cap.
    """

    max_tool_turns: int = _MAX_TOOL_TURNS
    max_tool_chars: int = _MAX_TOOL_CHARS
    max_tool_wallclock_s: float = _DEFAULT_TOOL_WALLCLOCK_S
    provider_rpm: int = _DEFAULT_PROVIDER_RPM
    provider_persistence: str = "disk"  # "disk" | "memory"

    @classmethod
    def from_config(cls, cfg: dict) -> "Limits":
        limits = cfg.get("limits", {}) or {}
        return cls(
            max_tool_turns=int(limits.get("max_tool_turns", _MAX_TOOL_TURNS)),
            max_tool_chars=int(limits.get("max_tool_chars", _MAX_TOOL_CHARS)),
            max_tool_wallclock_s=float(
                limits.get("max_tool_wallclock_s", _DEFAULT_TOOL_WALLCLOCK_S)
            ),
            provider_rpm=int(
                limits.get("provider_rpm", cfg.get("rate_limit", {}).get("provider_rpm", _DEFAULT_PROVIDER_RPM))
            ),
            provider_persistence=str(limits.get("provider_persistence", "disk")),
        )


# `_strip_think` and the `_THINK_RE` constants now live in
# `aaagent.core.agent_loop` so they can ship with the default loop
# implementation. The legacy shim re-exports them for any test that
# imported them from this module before the refactor.
from aaagent.core.agent_loop import (  # noqa: E402,F401
    AgentContext,
    AgentLoop,
    DefaultAgentLoop,
    _strip_think,
)
from aaagent.core.agent_loop import (  # noqa: E402,F401
    _THINK_RE,
    _UNCLOSED_THINK_RE,
    _PUBLIC_ERROR,
)


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


def _is_retryable_provider_error(exc: Exception, provider: Any | None = None) -> bool:
    """Best-effort classification of transient provider errors.

    When a `provider` instance is supplied, delegates to its
    `is_retryable_error()` hook (Provider protocol method). The legacy global
    classifier is kept here as a fallback for callers that don't have a
    provider handle, but new code should call `provider.is_retryable_error()`
    so vendor-specific signals live in the plugin that owns them.
    """
    if provider is not None and hasattr(provider, "is_retryable_error"):
        try:
            return bool(provider.is_retryable_error(exc))
        except Exception:  # noqa: BLE001
            pass
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
        providers: dict[str, _ProviderProtocol] | None = None,
        plugin_manager: PluginManager | None = None,
        agent_loop: AgentLoop | None = None,
    ) -> None:
        from dotenv import load_dotenv
        load_dotenv()
        self._config = self._load_config(config_path)
        self._config_store = ConfigStore(config_path)
        # Project root = directory containing config.yaml. All relative
        # paths declared in config.yaml resolve against this root so
        # behaviour does not depend on the operator's CWD.
        cfg_p = Path(config_path)
        self._project_root = (
            cfg_p.resolve().parent if cfg_p.is_absolute() else Path.cwd()
        )
        # Rewrite known path-typed keys to absolute paths anchored at
        # `_project_root` (paths.dotenv, memory.data_dir,
        # tools.allowed_dirs, limits.protected_paths, ...).
        resolve_all_paths(self._config, self._project_root)
        # Default paths.dotenv -> <project_root>/.env when unset, so
        # the dotenv store ends up next to config.yaml by default.
        if "dotenv" not in (self._config.get("paths", {}) or {}):
            self._config.setdefault("paths", {})["dotenv"] = str(
                self._project_root / ".env"
            )
        self._dotenv = DotenvStore(self._config["paths"]["dotenv"])
        self._bus = bus if bus is not None else EventBus()

        # Plugin discovery (loads entry_points + config overrides)
        self._plugins = plugin_manager if plugin_manager is not None else PluginManager(
            self._config
        )
        self._plugins.load()

        # Session / Memory via plugin factories. Direct injection via the
        # constructor parameter is still supported (used by tests); when no
        # session_store / memory is injected and no matching plugin is
        # registered, we raise rather than silently constructing a default.
        if session_store is None:
            session_cfg = self._config.get("session", {})
            session_type = session_cfg.get("type", "inmemory")
            factory = self._plugins.get_session_factory(session_type)
            session_store = factory.create(session_cfg)
        self._session_store = session_store
        self._adapters: list[IMAdapter] = []
        self._providers: dict[str, _ProviderProtocol] = providers if providers is not None else {}
        # `_providers_injected` distinguishes "user passed providers=... (use
        # exactly these, skip config.yaml setup)" from "user passed nothing,
        # fall back to config.yaml". An empty dict `{}` is a valid explicit
        # injection (no providers) and should skip `_setup_providers`.
        self._providers_injected = providers is not None
        self._provider: _ProviderProtocol | None = None
        # Set by _on_message_received so plugins (e.g. sqlite_session tools)
        # can derive the current platform / user_id without re-plumbing.
        self._last_message: Message | None = None
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
            factory = self._plugins.get_memory_factory(memory_type)
            memory = factory.create(memory_cfg)
        self._memory = memory
        _archive_hours = self._config.get("memory", {}).get("archive_after_hours", 24)
        self._archive_interval = float(_archive_hours or 24) * 3600
        self._tool_plugins: list[Any] = []
        self._tool_registry = tool_registry if tool_registry is not None else self._setup_tool_registry()
        self._enabled_adapters = enabled_adapters
        # DefaultAgentLoop takes a back-reference to this Application; the
        # loop reads `_tool_registry`, `_chat_with_fallback`, `_session_store`,
        # `_bus`, `_provider_order`, `_acquire_provider_bucket`, and `_limits`.
        # Plugins can supply their own loop (tree-of-thought, agent-as-tool,
        # ...) via the constructor parameter.
        self._agent_loop: AgentLoop = agent_loop or DefaultAgentLoop(self)
        # `_provider_rpm` is set by `Limits.from_config` above; legacy
        # `rate_limit.provider_rpm` is still honoured when present.
        self._provider_rpm = int(
            self._config.get("rate_limit", {}).get("provider_rpm", 0)
        )
        self._provider_buckets: dict[str, TokenBucket] = {}
        self._commands = SlashCommandRegistry()
        register_builtins(self._commands)
        # Plugins can ship slash commands via the `aaagent.commands` entry-point
        # group. Each entry point is a function `(app: Application) -> None`
        # that registers one or more commands on `app.commands`.
        for name, registrar in self._plugins.get_command_registrars().items():
            try:
                registrar(self)
            except Exception:  # noqa: BLE001
                logger.exception("Command registrar '%s' failed", name)
        # Resource caps (turns, wallclock, RPM, persistence) read
        # from `config.limits.*`. Provider RPM is still also read
        # from `rate_limit.provider_rpm` for backward compatibility.
        self._limits = Limits.from_config(self._config)
        # Wrap every logger handler so secret-bearing exception strings
        # never reach log files or stderr (Core 3 of capability limits).
        wrap_existing_handlers()
        self._setup()

    def _load_config(self, path: str) -> dict[str, Any]:
        p = Path(path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            cfg = yaml.safe_load(raw) or {}
            logger.debug("Loaded config (redacted):\n%s", scrub(raw))
            return cfg
        example = p.with_name("config.yaml.example")
        if example.exists():
            import shutil

            shutil.copy2(example, p)
            logger.warning(
                "%s not found; copied from %s — edit it before running /model.",
                p,
                example,
            )
            return self._load_config(path)
        return {}

    def _setup_tool_registry(self) -> ToolRegistry:
        tools_cfg = self._config.get("tools", {})
        allowed_dirs = tools_cfg.get("allowed_dirs", None)
        if allowed_dirs is None:
            # Default to the project root so `tools.allowed_dirs` is
            # implicit but explicit: same scope whether aaagent was
            # launched from project root or from anywhere else.
            allowed_dirs = [str(self._project_root)]
        else:
            validated: list[str] = []
            for d in allowed_dirs:
                # Re-resolve in case resolve_all_paths missed it (e.g.
                # absolute paths from the operator).
                p = resolve_project_path(d, self._project_root)
                if not p.exists():
                    logger.warning("allowed_dirs entry does not exist, skipped: %s", d)
                    continue
                validated.append(str(p))
            if not validated:
                logger.warning(
                    "No valid allowed_dirs after validation; defaulting to project root"
                )
                validated = [str(self._project_root)]
            allowed_dirs = validated

        registry = ToolRegistry(allowed_dirs=allowed_dirs)

        # Build the controlled plugin context once and hand it to every
        # tool plugin before registration. Plugins that need access to
        # the bus / memory store / project root read them from this
        # handle rather than probing the Application object — see
        # `aaagent.core.plugin.PluginContext`.
        ctx = PluginContext(
            event_bus=self._bus,
            session_store=self._session_store,
            memory_store=self._memory,
            project_root=self._project_root,
            config=self._config,
        )

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
                # `set_context` is a no-op on ToolPlugin base. Plugins
                # override it to capture fields they care about.
                plugin.set_context(ctx)
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
        # Only instantiate providers from config when none were injected
        # via the constructor. Passing `providers={}` (or any dict) is an
        # explicit "use exactly these and ignore config.yaml" signal.
        if not self._providers_injected:
            self._setup_providers()
        self._resolve_provider_chain()
        self._setup_adapters()
        self._setup_event_handlers()

    def _setup_providers(self) -> None:
        providers_cfg = self._config.get("providers", {})

        for name, cfg in providers_cfg.items():
            if name == "_meta":
                continue
            if not cfg.get("enabled", False):
                continue
            provider = self._instantiate_provider(name, cfg)
            if provider is None:
                continue
            self._providers[name] = provider
            logger.info("Loaded provider: %s (type=%s)", name, cfg.get("type", ""))

    def _instantiate_provider(
        self, name: str, cfg: dict[str, Any]
    ) -> _ProviderProtocol | None:
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
        return instance

    def _resolve_provider_chain(self) -> None:
        """Pick the primary provider and build the fallback order.

        `self._provider` is the primary used for latency-sensitive calls;
        `self._provider_order` is the ordered list consulted by
        `_chat_with_fallback` when the primary fails transiently.

        Routing data lives under `providers._meta` (default + fallback);
        top-level `default_provider` / `fallback_providers` are silently
        ignored to keep all provider config in one place.
        """
        providers_block = self._config.get("providers", {}) or {}
        meta = providers_block.get("_meta", {}) or {}
        default_name = meta.get("default", "")

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

        order: list[_ProviderProtocol] = []
        if self._provider is not None:
            order.append(self._provider)
        for name in meta.get("fallback", []) or []:
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

    async def _acquire_provider_bucket(self, provider: _ProviderProtocol) -> None:
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
                if not _is_retryable_provider_error(e, provider=provider):
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
        result: SlashResult = await self._commands.handle(
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

        self._last_message = msg
        tokens = set_context(
            session_id=msg.session_id,
            platform=msg.platform,
            chat_id=msg.chat_id,
            user_id=msg.user_id,
        )
        try:
            await self._handle_message(msg)
        finally:
            reset_context(tokens)

    async def _handle_message(self, msg: Message) -> None:
        """Persist the inbound message, hand off to the AgentLoop, persist the reply.

        The actual LLM/tool iteration lives in `self._agent_loop`. This
        method only orchestrates the per-request session writes and the
        `message_to_send` emission so an alternative loop can swap in
        without touching this code.
        """
        await self._session_store.add_message(msg.session_id, msg)

        session = await self._session_store.get_session(msg.session_id)
        profile = await self._memory.recall_profile()
        builder = PromptBuilder(system_prompt=self._session_store._system_prompt)
        messages = builder.build(session, profile=profile)

        context = AgentContext(
            session_id=msg.session_id,
            platform=msg.platform,
            chat_id=msg.chat_id,
            messages=messages,
            tools=self._tool_registry.definitions,
            system_prompt=self._session_store._system_prompt,
            profile=profile,
        )

        reply_text = await self._agent_loop.handle_message(msg, context)

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

    def add_adapter(self, adapter: IMAdapter) -> None:
        self._adapters.append(adapter)

    @property
    def commands(self) -> SlashCommandRegistry:
        """Public handle to the slash-command registry.

        Plugins can call `app.commands.register(name, description=..., handler=..., source="<plugin-name>")`
        to contribute their own commands at startup. Use the helper
        `register_slash_command` for the common case.
        """
        return self._commands

    def register_slash_command(
        self,
        name: str,
        *,
        description: str,
        handler,
        source: str,
    ) -> None:
        """Convenience wrapper around `commands.register`.

        `source` should be the distributing plugin's name (e.g. "shell")
        so `/help` can attribute each command.
        """
        self._commands.register(
            name,
            description=description,
            handler=handler,
            source=source,
        )

    def set_provider(self, provider: _ProviderProtocol) -> None:
        self._provider = provider
        self._provider_order = [provider]
        self._init_provider_buckets()

    def get_provider(self, name: str) -> _ProviderProtocol | None:
        return self._providers.get(name)

    @property
    def provider(self) -> _ProviderProtocol | None:
        return self._provider

    async def run(self) -> None:
        if not self._adapters:
            logger.error("No adapters configured, nothing to run")
            return

        self._health_task = asyncio.create_task(self._health_check_loop())
        if self._memory is not None and self._archive_interval > 0:
            self._archive_task = asyncio.create_task(self._archive_sweep_loop())
        await self._establish_tool_plugins()
        warmup = getattr(self._session_store, "warmup", None)
        if warmup is not None:
            try:
                await warmup()
            except Exception as e:  # noqa: BLE001
                logger.warning("session store warmup failed: %s", e)
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
        # Drain the session store's pending async writes (notably the
        # _DualWriteSessionStore pending tasks backed by aiosqlite
        # worker threads). Without this, the aiosqlite threads block
        # loop.close() and Ctrl+C hangs in idle mode.
        close_store = getattr(self._session_store, "close", None)
        if close_store is not None:
            try:
                res = close_store()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("session store close timed out after 2s")
            except Exception as e:  # noqa: BLE001
                logger.error("session store close failed: %s", e)
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
