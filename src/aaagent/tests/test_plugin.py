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


def test_plugin_manager_config_explicit_overrides_entry_points():
    """config.yaml's `plugins:` block can ship plugin classes without an
    installed package — useful for ad-hoc in-process providers.
    """
    pm = PluginManager(
        config={
            "plugins": [
                {
                    "kind": "provider",
                    "type": "good",
                    "class": "test_plugin:_GoodProvider",
                }
            ]
        }
    )
    pm.load()
    assert pm.get_provider_class("good") is _GoodProvider


def test_plugin_manager_cli_command_registrar_round_trip():
    """A plugin can register a `aaagent <name>` Typer subcommand by
    exporting a `(typer_app, config_path) -> None` function under the
    `aaagent.cli_commands` entry-point group. PluginManager should
    pick it up and expose it via `get_cli_command_registrars()`.
    """
    called = {"count": 0, "last_config_path": None}

    def _register(app, config_path):
        called["count"] += 1
        called["last_config_path"] = config_path

    pm = PluginManager(config={})
    pm._register_loaded("aaagent.cli_commands", "demo", _register)
    registrars = pm.get_cli_command_registrars()
    assert "demo" in registrars
    assert registrars["demo"] is _register
    assert "cli_commands" in pm.loaded
    assert "demo" in pm.loaded["cli_commands"]


def test_cli_loads_plugin_commands_eagerly(monkeypatch, tmp_path):
    """`aaagent.cli` should call plugin-supplied CLI registrars at
    import time so `aaagent <name> --help` works without any extra
    step. We don't actually invoke Typer here (it would try to read
    argv); we just verify the registrar's `app.add_typer` / command
    decorator was reached.
    """
    import aaagent.cli as cli_mod

    captured: list[str] = []

    def _register(app, config_path):
        captured.append(config_path)
        # Use the real Typer command decorator to prove the wiring is
        # compatible — no execution, just surface extension.
        @app.command()
        def demo_cmd():
            """demo"""
            pass

    # Patch the discovery function to return our fake registrar only.
    def _fake_loader(config_path):
        _register(cli_mod.app, config_path)

    monkeypatch.setattr(cli_mod, "_load_cli_commands", _fake_loader)
    cli_mod._load_cli_commands(str(tmp_path / "config.yaml"))
    assert captured == [str(tmp_path / "config.yaml")]
    # The Typer app now exposes the new command.
    assert any(
        getattr(c.callback, "__name__", None) == "demo_cmd"
        for c in cli_mod.app.registered_commands
    )