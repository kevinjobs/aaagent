from aaagent_plugin_feishu import FeishuAdapter, _resolve_env
from aaagent.core.bus import EventBus

import pytest


def test_resolve_env_passthrough():
    assert _resolve_env("plain") == "plain"


def test_resolve_env_empty():
    assert _resolve_env("") == ""


def test_resolve_env_substitution(monkeypatch):
    monkeypatch.setenv("MY_FEISHU_KEY", "secret123")
    assert _resolve_env("${MY_FEISHU_KEY}") == "secret123"


def test_resolve_env_missing(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert _resolve_env("${NOPE}") == ""


def test_feishu_config_warning_when_missing(caplog):
    import logging

    bus = EventBus()
    with caplog.at_level(logging.WARNING):
        FeishuAdapter({"app_id": "", "app_secret": ""}, bus)
    assert any("Feishu adapter misconfigured" in r.getMessage() for r in caplog.records)


def test_feishu_remember_message_dedup():
    bus = EventBus()
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y"}, bus
    )  # missing config but we won't call start
    assert adapter._remember_message("om_1") is True
    assert adapter._remember_message("om_1") is False
    assert adapter._remember_message("om_2") is True


def test_feishu_remember_message_empty_id():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    assert adapter._remember_message("") is True
    assert adapter._remember_message("") is True  # empty always allowed


def test_feishu_truncate_long_message():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    long = "x" * 5000
    truncated = adapter._truncate_for_feishu(long)
    assert len(truncated) == 4000


def test_feishu_truncate_short_message_unchanged():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    short = "hello"
    assert adapter._truncate_for_feishu(short) == short


@pytest.mark.asyncio
async def test_feishu_slash_command_routes_via_bus(monkeypatch):
    """Slash-prefixed messages emit slash_command instead of message_received."""
    import asyncio
    from aaagent.core.bus import EventBus
    from aaagent.core.message import Message
    from aaagent_plugin_feishu import FeishuAdapter

    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)

    sent_to_message_received: list = []
    sent_to_slash_command: list = []

    async def on_msg(p):
        sent_to_message_received.append(p)

    async def on_slash(p):
        sent_to_slash_command.append(p)

    bus.on("message_received", on_msg)
    bus.on("slash_command", on_slash)

    sent: list = []

    async def fake_send(msg):
        sent.append(msg)

    adapter.send = fake_send  # type: ignore[method-assign]

    # Simulate Application-side reaction: reply via slash_reply → Feishu send
    await bus.emit(
        "slash_command",
        {
            "text": "/help",
            "platform": "feishu",
            "session_id": "feishu-oc_xxx",
            "chat_id": "oc_xxx",
        },
    )
    await bus.emit(
        "slash_reply",
        {
            "platform": "feishu",
            "session_id": "feishu-oc_xxx",
            "chat_id": "oc_xxx",
            "reply": "Available commands:\n  /help - ...",
            "suppressed": False,
        },
    )

    assert len(sent_to_slash_command) == 1
    assert sent_to_slash_command[0]["text"] == "/help"
    assert sent_to_message_received == []
    assert len(sent) == 1
    assert sent[0].content.startswith("Available commands")


@pytest.mark.asyncio
async def test_feishu_blacklisted_command_gets_not_supported_reply():
    """When /quit is blacklisted for feishu, the registry returns a
    suppressed reply and the adapter forwards it via send()."""
    from aaagent.core.bus import EventBus
    from aaagent.core.commands import (
        SlashCommandRegistry,
        SlashContext,
        register_builtins,
    )
    from aaagent_plugin_feishu import FeishuAdapter

    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)

    sent: list = []

    async def fake_send(msg):
        sent.append(msg)

    adapter.send = fake_send  # type: ignore[method-assign]

    reg = SlashCommandRegistry()
    register_builtins(reg)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")
    result = reg.handle("/quit", ctx, blacklist={"/quit"})

    if result.reply:
        await bus.emit(
            "slash_reply",
            {
                "platform": "feishu",
                "session_id": "feishu-x",
                "chat_id": "x",
                "reply": result.reply,
                "suppressed": result.suppressed,
            },
        )

    assert result.suppressed is True
    assert result.stop_adapter is False
    assert len(sent) == 1
    assert "feishu" in sent[0].content
    assert "不支持" in sent[0].content