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
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from aaagent.core.bus import EventBus
    from aaagent.core.memory import MemoryStore
    from aaagent.core.message import Message
    from aaagent.core.session import SessionStore
    from aaagent.core.types import ChatResponse
    from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent.plugin")

PROVIDER_GROUP = "aaagent.providers"
TOOL_GROUP = "aaagent.tools"
ADAPTER_GROUP = "aaagent.adapters"
SESSION_GROUP = "aaagent.sessions"
MEMORY_GROUP = "aaagent.memories"


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
    """Discovers and validates plugins from entry_points + builtin registry + config.

    Discovery layers (later overrides earlier):
        1. builtin registry (BUILTIN_* class dicts)
        2. Python entry points (`importlib.metadata.entry_points(group=...)`)
        3. config.yaml explicit `plugins:` declarations

    After all loading, `_validate_all()` checks each registered class has
    the required attributes and methods, raising `PluginValidationError`
    on the first failure.
    """

    BUILTIN_PROVIDERS: dict[str, str] = {}
    BUILTIN_TOOLS: dict[str, str] = {}
    BUILTIN_ADAPTERS: dict[str, str] = {}
    BUILTIN_SESSIONS: dict[str, str] = {}
    BUILTIN_MEMORIES: dict[str, str] = {}

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._provider_classes: dict[str, type[Provider]] = {}
        self._tool_classes: dict[str, type[ToolPlugin]] = {}
        self._adapter_classes: dict[str, type[IMAdapter]] = {}
        self._session_factories: dict[str, SessionStoreFactory] = {}
        self._memory_factories: dict[str, MemoryStoreFactory] = {}

    def load(self) -> None:
        self._load_builtin()
        self._load_entry_points()
        self._load_config_explicit()
        self._validate_all()

    def _load_builtin(self) -> None:
        for type_name, dotted in self.BUILTIN_PROVIDERS.items():
            self._register_class(PROVIDER_GROUP, type_name, dotted)
        for name, dotted in self.BUILTIN_TOOLS.items():
            self._register_class(TOOL_GROUP, name, dotted)
        for name, dotted in self.BUILTIN_ADAPTERS.items():
            self._register_class(ADAPTER_GROUP, name, dotted)
        for name, dotted in self.BUILTIN_SESSIONS.items():
            self._register_class(SESSION_GROUP, name, dotted)
        for name, dotted in self.BUILTIN_MEMORIES.items():
            self._register_class(MEMORY_GROUP, name, dotted)

    def _load_entry_points(self) -> None:
        import importlib.metadata as md

        groups = (
            PROVIDER_GROUP,
            TOOL_GROUP,
            ADAPTER_GROUP,
            SESSION_GROUP,
            MEMORY_GROUP,
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

    @property
    def loaded(self) -> dict[str, list[str]]:
        return {
            "providers": sorted(self._provider_classes),
            "tools": sorted(self._tool_classes),
            "adapters": sorted(self._adapter_classes),
            "sessions": sorted(self._session_factories),
            "memories": sorted(self._memory_factories),
        }