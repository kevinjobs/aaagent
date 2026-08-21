from __future__ import annotations

from typing import Any

import pytest

from aaagent.core.envutil import resolve_env, resolve_env_dict
from aaagent.core.plugin import (
    MemoryStoreFactory,
    PluginManager,
    PluginNotFoundError,
    PluginValidationError,
    Provider,
    SessionStoreFactory,
    ToolPlugin,
)


class _GoodProvider(Provider):
    type = "good"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

    async def chat(self, messages, tools=None, **kwargs):
        from aaagent.core.types import ChatResponse

        return ChatResponse(content="ok")


class _BadProvider:
    # Missing `type`, missing `chat`
    pass


class _GoodTool(ToolPlugin):
    name = "good_tool"

    def register(self, registry, config):
        pass


class _OverrideProvider(Provider):
    type = "x"

    async def chat(self, messages, tools=None, **kwargs):
        from aaagent.core.types import ChatResponse

        return ChatResponse(content="override")


class _BadTool:
    """Tool plugin missing the `register` method."""
    pass


def test_resolve_env_expands_placeholder(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert resolve_env("hello-${FOO}-baz") == "hello-bar-baz"


def test_resolve_env_returns_empty_for_missing(monkeypatch):
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
    assert resolve_env("x-${MISSING_VAR_XYZ}-y") == "x--y"


def test_resolve_env_passes_non_strings():
    assert resolve_env(123) == 123
    assert resolve_env(None) is None
    assert resolve_env([1, 2]) == [1, 2]


def test_resolve_env_dict_shallow():
    out = resolve_env_dict({"a": "${X}", "b": 5})
    assert out["a"] == ""
    assert out["b"] == 5


def test_plugin_manager_loads_entry_points_even_without_builtin():
    """entry_points discovery runs even when BUILTIN_* dicts are empty."""
    pm = PluginManager(config={})
    pm.BUILTIN_PROVIDERS = {}
    pm.BUILTIN_TOOLS = {}
    pm.BUILTIN_ADAPTERS = {}
    pm.BUILTIN_SESSIONS = {}
    pm.BUILTIN_MEMORIES = {}
    pm.load()
    # entry_points for installed plugins (e.g. inmemorysession) should be visible
    assert "inmemory" in pm.loaded["sessions"]


def test_plugin_manager_validates_missing_type():
    pm = PluginManager(config={})
    pm._provider_classes["bad"] = _BadProvider
    with pytest.raises(PluginValidationError, match="missing 'type'"):
        pm._validate_all()


def test_plugin_manager_validates_missing_method():
    pm = PluginManager(config={})
    pm._tool_classes["bad"] = _BadTool
    with pytest.raises(PluginValidationError, match="missing 'register'"):
        pm._validate_all()


def test_plugin_manager_get_provider_raises_with_hint():
    pm = PluginManager(config={})
    pm.load()
    with pytest.raises(PluginNotFoundError, match="aaagent-plugin"):
        pm.get_provider_class("nonexistent")


def test_plugin_manager_get_session_factory_raises():
    pm = PluginManager(config={})
    pm.load()
    with pytest.raises(PluginNotFoundError, match="aaagent-plugin-inmemorysession"):
        pm.get_session_factory("nonexistent")


def test_plugin_manager_get_memory_factory_raises():
    pm = PluginManager(config={})
    pm.load()
    with pytest.raises(PluginNotFoundError, match="aaagent-plugin-markdownstore"):
        pm.get_memory_factory("nonexistent")


def test_plugin_manager_builtin_registration():
    pm = PluginManager(config={})
    pm.BUILTIN_PROVIDERS = {"good": "test_plugin:_GoodProvider"}
    pm.BUILTIN_TOOLS = {"good_tool": "test_plugin:_GoodTool"}
    pm.load()
    assert "good" in pm.loaded["providers"]
    assert "good_tool" in pm.loaded["tools"]
    cls = pm.get_provider_class("good")
    assert cls is _GoodProvider


def test_plugin_manager_config_explicit_overrides_builtin():
    pm = PluginManager(
        config={
            "plugins": [
                {
                    "kind": "provider",
                    "type": "good",
                    "class": "test_plugin:_OverrideProvider",
                }
            ]
        }
    )
    pm.BUILTIN_PROVIDERS = {"good": "test_plugin:_GoodProvider"}
    pm.load()
    assert pm.get_provider_class("good") is _OverrideProvider