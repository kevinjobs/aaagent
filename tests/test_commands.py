from __future__ import annotations

from types import SimpleNamespace

import pytest

from aaagent.core.commands import (
    SlashCommandRegistry,
    SlashContext,
    SlashResult,
    register_builtins,
)


def _ctx(reg: SlashCommandRegistry | None = None, platform: str = "cli", session_id: str = "cli-default") -> SlashContext:
    app = SimpleNamespace(_commands=reg) if reg is not None else None
    return SlashContext(
        platform=platform, session_id=session_id, chat_id=session_id, app=app
    )


def test_register_rejects_invalid_name():
    reg = SlashCommandRegistry()
    with pytest.raises(ValueError, match="must start with '/'"):
        reg.register("foo", description="x", handler=lambda *a, **k: SlashResult())
    with pytest.raises(ValueError, match="must not contain spaces"):
        reg.register("/foo bar", description="x", handler=lambda *a, **k: SlashResult())


def test_handle_ignores_non_slash():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("hello world", _ctx())
    assert result.matched is False
    assert result.reply is None


def test_handle_unknown_command():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/nope arg", _ctx())
    assert result.matched is False


def test_help_lists_builtins():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/help", _ctx(reg=reg))
    assert result.matched is True
    assert "/help" in (result.reply or "")
    assert "/quit" in (result.reply or "")
    assert "/session" in (result.reply or "")
    assert "/sessions" in (result.reply or "")
    # /exit removed; should not appear in help
    assert "/exit" not in (result.reply or "")


def test_quit_signals_stop_adapter():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/quit", _ctx())
    assert result.matched is True
    assert result.stop_adapter is True
    assert result.reply == "Bye."


def test_exit_no_longer_registered():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/exit", _ctx())
    assert result.matched is False


def test_session_no_arg_starts_new_session():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/session", _ctx())
    assert result.matched is True
    assert result.switch_session is not None
    assert result.switch_session.startswith("cli-new-")
    assert "Started new session" in (result.reply or "")


def test_session_with_arg_switches():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/session bar", _ctx())
    assert result.matched is True
    assert result.switch_session == "cli-bar"
    assert "Switched to session" in (result.reply or "")


def test_session_strips_already_prefixed_arg():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/session cli-bar", _ctx())
    assert result.switch_session == "cli-bar"


def test_session_same_id_noops():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/session cli-default", _ctx(session_id="cli-default"))
    assert result.matched is True
    assert result.switch_session is None
    assert "Already in session" in (result.reply or "")


def test_sessions_lists_with_current_marker():
    from types import SimpleNamespace

    fake_session_a = SimpleNamespace(
        id="cli-default", last_activity=100, messages=[1, 2, 3], summary=""
    )
    fake_session_b = SimpleNamespace(
        id="cli-foo", last_activity=200, messages=[], summary="old chat"
    )
    fake_store = SimpleNamespace(list_sessions=lambda: [fake_session_a, fake_session_b])
    app = SimpleNamespace(_commands=None, _session_store=fake_store)
    ctx = SlashContext(platform="cli", session_id="cli-default", chat_id="x", app=app)

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "Sessions:" in (result.reply or "")
    assert "* cli-default" in (result.reply or "")
    assert " cli-foo" in (result.reply or "")
    assert "[summary: old chat" in (result.reply or "")


def test_sessions_empty():
    fake_store = SimpleNamespace(list_sessions=lambda: [])
    app = SimpleNamespace(_commands=None, _session_store=fake_store)
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "No sessions yet" in (result.reply or "")


def test_sessions_no_store_graceful():
    app = SimpleNamespace(_commands=None, _session_store=None)
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "unavailable" in (result.reply or "")


def test_blacklisted_command_returns_unsupported_reply():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")
    result = reg.handle("/quit", ctx, blacklist={"/quit"})
    assert result.matched is True
    assert result.suppressed is True
    assert "feishu" in (result.reply or "")
    assert result.stop_adapter is False
    assert result.switch_session is None


def test_blacklisted_command_drops_side_effects_even_if_handler_returns_them():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")

    def evil(arg, ctx):
        return SlashResult(matched=True, stop_adapter=True, switch_session="evil")

    reg.register("/evil", description="evil", handler=evil)
    result = reg.handle("/evil", ctx, blacklist={"/evil"})
    assert result.suppressed is True
    assert result.stop_adapter is False
    assert result.switch_session is None


def test_handler_exception_is_caught():
    reg = SlashCommandRegistry()

    def boom(arg, ctx):
        raise RuntimeError("nope")

    reg.register("/boom", description="boom", handler=boom)
    result = reg.handle("/boom", _ctx())
    assert result.matched is True
    assert "nope" in (result.reply or "")
    assert result.stop_adapter is False


def test_custom_command_registration():
    reg = SlashCommandRegistry()
    register_builtins(reg)

    def echo(arg, ctx):
        return SlashResult(reply=f"echo: {arg}")

    reg.register("/echo", description="Echo input", handler=echo)
    result = reg.handle("/echo hello", _ctx())
    assert result.matched is True
    assert result.reply == "echo: hello"


def test_list_commands_sorted_by_name():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    reg.register("/aaa", description="first", handler=lambda *a, **k: SlashResult())
    names = [n for n, _ in reg.list_commands()]
    assert names == sorted(names)
    assert "/aaa" in names
    assert "/help" in names


def test_case_insensitive_command_lookup():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = reg.handle("/QUIT", _ctx())
    assert result.matched is True
    assert result.stop_adapter is True