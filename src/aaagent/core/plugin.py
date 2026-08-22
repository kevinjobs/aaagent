"""Plugin protocol and discovery for aaagent.

The plugin system uses Python entry points (`importlib.metadata.entry_points`)
as the standard discovery mechanism. Plugins are organised into five groups:

- `aaagent.providers`  — LLM provider implementations
- `aaagent.tools`      — Tool plugins (register one or more tools)
- `aaagent.adapters`   — IM channel adapters
- `aaagent.sessions`   — SessionStore factories
- `aaagent.memories`   — MemoryStore factories

Each entry-point value is `"<dotted.module.path>:<ClassName>"` and resolves
to a class that subclasses one of the protocols / ABCs defined here.
"""

from __future__ import annotations

import abc
import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

if TYPE_CHECKING:
    from aaagent.core.bus import EventBus
    from aaagent.core.memory import MemoryStore
    from aaagent.core.session import SessionStore


@dataclass
class PluginContext:
    """Controlled view into the live Application a plugin needs at registration.

    Plugins must not hold a reference to the `Application` object itself —
    they receive this immutable handle once, at registration time, and read
    what they need from it. Adding a field here is the only way to widen
    the plugin-visible API; the core owns this contract.

    Fields:
        event_bus:      the framework's EventBus (slack_reply, tool_result, ...)
        session_store:  the configured SessionStore
        memory_store:   the configured MemoryStore (may be None if no plugin
                        installed)
        project_root:   absolute Path to the directory containing config.yaml
        config:         the parsed config.yaml dict (read-only contract — plugins
                        should not mutate it; use SlashCommandRegistry /
                        ConfigStore for changes)
    """

    event_bus: "EventBus"
    session_store: "SessionStore"
    memory_store: "MemoryStore | None"
    project_root: Path
    config: dict[str, Any]


_DEFAULT_RETRYABLE_MARKERS = (
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
    # is more aggressive than DeepSeek / 9router). These substrings are
    # signal-specific, not vendor-specific, so the universal default
    # classifier owns them.
    "sensitive",
    "unprocessable_entity",
    "content_filter",
    "content_policy_violation",
    "policy_violation",
)


def _default_is_retryable(exc: BaseException) -> bool:
    """Conservative default classifier — used by Provider.is_retryable_error.

    Only covers universal transient signals (network, timeout, OS-level) plus
    a substring sweep over the universal HTTP / rate-limit vocabulary. Provider
    plugins that need to recognise their SDK's named exception classes
    override `is_retryable_error()` and may combine this default via
    `super().is_retryable_error(exc) or <vendor-specific check>`.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _DEFAULT_RETRYABLE_MARKERS)


if TYPE_CHECKING:
    from aaagent.core.message import Message
    from aaagent.core.types import ChatResponse
    from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent.plugin")

PROVIDER_GROUP = "aaagent.providers"
TOOL_GROUP = "aaagent.tools"
ADAPTER_GROUP = "aaagent.adapters"
SESSION_GROUP = "aaagent.sessions"
MEMORY_GROUP = "aaagent.memories"
COMMAND_GROUP = "aaagent.commands"


class PluginNotFoundError(LookupError):
    """Raised when a plugin type is requested but not registered."""


class PluginValidationError(RuntimeError):
    """Raised when a plugin class fails runtime validation."""


class Provider(abc.ABC):
    """LLM provider plugin interface.

    Subclasses must define the class attribute `type` (the unique identifier
    used in config.yaml's `providers.<name>.type` field) and implement
    `chat()`.
    """

    type: str

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        # The framework writes the config-block name (e.g. the YAML key
        # under `providers:`) into `config["_name"]` before
        # instantiating, so any subclass that relies on `self.name`
        # works without needing its own `__init__` plumbing. Plugins
        # that want to override `name` later can still do so.
        self.name: str = str(config.get("_name", "") or "")

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> "ChatResponse":
        ...

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        raise NotImplementedError(
            f"{type(self).__name__} does not implement stream_chat"
        )
        yield ""  # pragma: no cover

    def is_retryable_error(self, exc: BaseException) -> bool:
        """Best-effort classification of transient provider errors.

        Returning True tells `Application._chat_with_fallback` / `_stream_or_chat`
        to try the next provider in the chain. Returning False aborts the
        fallback and surfaces the error immediately (typically a non-transient
        condition: bad request, auth failure, content-policy violation that
        the operator wants to see).

        The base implementation covers the universal case (network / timeout /
        OS-level) and a conservative string-marker sweep that catches common
        transient substrings (HTTP 429/5xx, "rate limit", "timeout", ...).
        Provider plugins that talk to vendor-specific SDKs (OpenAI,
        Anthropic, MiniMax, ...) override this to plug in their SDK's named
        exception classes without forcing the core to know those class names.
        """
        return _default_is_retryable(exc)


class ToolPlugin(abc.ABC):
    """A plugin that registers one or more tools with the ToolRegistry."""

    name: str

    @abc.abstractmethod
    def register(
        self,
        registry: "ToolRegistry",
        config: dict[str, Any],
    ) -> None:
        ...

    def set_context(self, ctx: PluginContext) -> None:
        """Receive the framework-level handle the plugin can read from.

        Called once by `Application._setup_tool_registry` after the
        plugin is instantiated and before `register()`. The base
        implementation is a no-op; override to capture fields like
        `ctx.memory_store` / `ctx.event_bus` / `ctx.project_root`.

        This single, explicit hook replaces the old `set_memory` /
        `set_application` ad-hoc probes. Adding a new plugin-visible
        capability means adding a field to `PluginContext`.
        """
        return None

    async def establish(
        self,
        registry: "ToolRegistry",
        config: dict[str, Any],
    ) -> None:
        """Optional async setup (e.g. connect to external services).

        Called once by Application.run() before adapters start, after the
        synchronous `register()` phase. Backed by a no-op default so tool
        plugins that only need sync registration can ignore it.
        """
        return None

    async def close(self) -> None:
        """Optional async cleanup invoked at Application.stop()."""
        return None


class IMAdapter(abc.ABC):
    """IM channel adapter plugin interface."""

    name: str

    def __init__(self, config: dict[str, Any], bus: "EventBus") -> None:
        self.config = config
        self.bus = bus

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, msg: "Message") -> None: ...

    async def health_check(self) -> bool:
        return True


class SessionStoreFactory(abc.ABC):
    """Provides SessionStore instances.

    `name` is the entry-point name (used in config.yaml's `session.type`).
    """

    name: str

    @abc.abstractmethod
    def create(self, config: dict[str, Any]) -> "SessionStore":
        ...


class MemoryStoreFactory(abc.ABC):
    """Provides MemoryStore instances."""

    name: str

    @abc.abstractmethod
    def create(self, config: dict[str, Any]) -> "MemoryStore":
        ...


class PluginManager:
    """Discovers and validates plugins from entry_points + config.

    Discovery layers (later overrides earlier):
        1. Python entry points (`importlib.metadata.entry_points(group=...)`)
        2. config.yaml explicit `plugins:` declarations

    After all loading, `_validate_all()` checks each registered class has
    the required attributes and methods, raising `PluginValidationError`
    on the first failure.

    The core no longer carries a built-in registry of plugin classes. The
    `pip install aaagent[default]` extra (see `pyproject.toml`) is what pulls
    in the plugins used by the example config; everything else must be installed
    explicitly. This keeps the core's knowledge of plugin classes to zero.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._provider_classes: dict[str, type[Provider]] = {}
        self._tool_classes: dict[str, type[ToolPlugin]] = {}
        self._adapter_classes: dict[str, type[IMAdapter]] = {}
        self._session_factories: dict[str, SessionStoreFactory] = {}
        self._memory_factories: dict[str, MemoryStoreFactory] = {}
        self._command_registrars: dict[str, Callable[[Any], None]] = {}

    def load(self) -> None:
        self._load_entry_points()
        self._load_config_explicit()
        self._validate_all()

    def _load_entry_points(self) -> None:
        import importlib.metadata as md

        groups = (
            PROVIDER_GROUP,
            TOOL_GROUP,
            ADAPTER_GROUP,
            SESSION_GROUP,
            MEMORY_GROUP,
            COMMAND_GROUP,
        )
        for group in groups:
            try:
                eps = md.entry_points(group=group)
            except TypeError:
                # Python 3.9 fallback
                all_eps = md.entry_points()
                eps = all_eps.select(group=group) if hasattr(all_eps, "select") else all_eps.get(group, [])
            for ep in eps:
                try:
                    cls = ep.load()
                except Exception as e:  # noqa: BLE001
                    logger.error("Failed to load entry point %s: %s", ep.name, e)
                    continue
                self._register_loaded(group, ep.name, cls)

    def _load_config_explicit(self) -> None:
        for cfg in self._config.get("plugins", []) or []:
            kind = cfg.get("kind")
            type_name = cfg.get("type") or cfg.get("name")
            dotted = cfg.get("class")
            if not kind or not type_name or not dotted:
                logger.warning(
                    "Skipping malformed plugin config (need kind/type/class): %s", cfg
                )
                continue
            self._register_class(f"aaagent.{kind}s", type_name, dotted)

    def _register_class(self, group: str, name: str, dotted: str) -> None:
        try:
            module_path, _, cls_name = dotted.rpartition(":")
            module = importlib.import_module(module_path)
            cls = getattr(module, cls_name)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Failed to import plugin class %s for %s/%s: %s",
                dotted,
                group,
                name,
                e,
            )
            return
        self._register_loaded(group, name, cls)

    def _register_loaded(self, group: str, name: str, cls: type) -> None:
        if group == PROVIDER_GROUP:
            self._provider_classes[name] = cls
        elif group == TOOL_GROUP:
            self._tool_classes[name] = cls
        elif group == ADAPTER_GROUP:
            self._adapter_classes[name] = cls
        elif group == SESSION_GROUP:
            self._session_factories[name] = cls() if isinstance(cls, type) else cls
        elif group == MEMORY_GROUP:
            self._memory_factories[name] = cls() if isinstance(cls, type) else cls
        elif group == COMMAND_GROUP:
            self._command_registrars[name] = cls  # type: ignore[assignment]

    def _validate_all(self) -> None:
        for type_name, cls in self._provider_classes.items():
            if not getattr(cls, "type", None):
                raise PluginValidationError(
                    f"Provider {cls.__module__}.{cls.__name__} missing 'type' class attribute"
                )
            if not callable(getattr(cls, "chat", None)):
                raise PluginValidationError(
                    f"Provider '{type_name}' ({cls.__module__}.{cls.__name__}) "
                    f"missing 'chat' method"
                )
        for name, cls in self._tool_classes.items():
            if not callable(getattr(cls, "register", None)):
                raise PluginValidationError(
                    f"ToolPlugin '{name}' ({cls.__module__}.{cls.__name__}) "
                    f"missing 'register' method"
                )
        for name, cls in self._adapter_classes.items():
            for method in ("start", "stop", "send"):
                if not callable(getattr(cls, method, None)):
                    raise PluginValidationError(
                        f"IMAdapter '{name}' ({cls.__module__}.{cls.__name__}) "
                        f"missing '{method}' method"
                    )
        for name, factory in self._memory_factories.items():
            if not callable(getattr(factory, "create", None)):
                raise PluginValidationError(
                    f"MemoryStoreFactory '{name}' missing 'create' method"
                )
        for name, factory in self._session_factories.items():
            if not callable(getattr(factory, "create", None)):
                raise PluginValidationError(
                    f"SessionStoreFactory '{name}' missing 'create' method"
                )

    def get_provider_class(self, type_name: str) -> type[Provider]:
        cls = self._provider_classes.get(type_name)
        if cls is None:
            raise PluginNotFoundError(
                f"No provider plugin for type '{type_name}'. "
                f"Install one with `pip install aaagent-plugin-<provider-name>`."
            )
        return cls

    def get_tool_classes(self) -> list[type[ToolPlugin]]:
        return list(self._tool_classes.values())

    def get_adapter_class(self, name: str) -> type[IMAdapter] | None:
        return self._adapter_classes.get(name)

    def get_session_factory(self, type_name: str) -> SessionStoreFactory:
        factory = self._session_factories.get(type_name)
        if factory is None:
            raise PluginNotFoundError(
                f"No session store plugin for type '{type_name}'. "
                f"Install one with `pip install aaagent-plugin-inmemorysession`."
            )
        return factory

    def get_memory_factory(self, type_name: str) -> MemoryStoreFactory:
        factory = self._memory_factories.get(type_name)
        if factory is None:
            raise PluginNotFoundError(
                f"No memory store plugin for type '{type_name}'. "
                f"Install one with `pip install aaagent-plugin-markdownstore`."
            )
        return factory

    def get_command_registrars(self) -> dict[str, Callable[[Any], None]]:
        """Return the loaded slash-command registrars keyed by entry-point name.

        Each value is a callable that takes the live `Application` instance
        and registers one or more slash commands on its `commands` registry.
        Plugins that ship slash commands expose a function in the
        `aaagent.commands` entry-point group; the core calls them at
        startup, after `register_builtins`.
        """
        return dict(self._command_registrars)

    @property
    def loaded(self) -> dict[str, list[str]]:
        return {
            "providers": sorted(self._provider_classes),
            "tools": sorted(self._tool_classes),
            "adapters": sorted(self._adapter_classes),
            "sessions": sorted(self._session_factories),
            "memories": sorted(self._memory_factories),
        }