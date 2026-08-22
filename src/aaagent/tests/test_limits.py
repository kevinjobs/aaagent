from __future__ import annotations

import asyncio

import pytest

from aaagent.core.app import Limits
from aaagent.core.bus import EventBus
from aaagent.core.message import Message
from aaagent.core.types import ChatResponse, LLMProvider, ToolCall
from aaagent_plugin_markdownstore import MarkdownMemoryStore


def _write_cfg(tmp_path, *, max_tool_turns: int | None = None, max_tool_wallclock_s: float | None = None):
    cfg = tmp_path / "config.yaml"
    lines = [
        "providers:",
        "  _meta:",
        "    default: fake",
        "  fake:",
        "    type: custom",
        "    class: aaagent.testing.FakeProvider",
        "    enabled: true",
        "tools:",
        "  shell:",
        "    enabled: false",
    ]
    if max_tool_turns is not None or max_tool_wallclock_s is not None:
        lines.append("limits:")
        if max_tool_turns is not None:
            lines.append(f"  max_tool_turns: {max_tool_turns}")
        if max_tool_wallclock_s is not None:
            lines.append(f"  max_tool_wallclock_s: {max_tool_wallclock_s}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


def test_limits_defaults_when_no_config_section():
    limits = Limits.from_config({})
    assert limits.max_tool_turns == 10
    assert limits.max_tool_chars == 200_000
    assert limits.max_tool_wallclock_s == 120
    assert limits.provider_rpm == 30
    assert limits.provider_persistence == "disk"


def test_limits_reads_from_config():
    cfg = {
        "limits": {
            "max_tool_turns": 5,
            "max_tool_wallclock_s": 60,
            "provider_rpm": 12,
            "provider_persistence": "memory",
        }
    }
    limits = Limits.from_config(cfg)
    assert limits.max_tool_turns == 5
    assert limits.max_tool_wallclock_s == 60
    assert limits.provider_rpm == 12
    assert limits.provider_persistence == "memory"


def test_limits_legacy_rate_limit_provider_rpm_still_honoured():
    """Backwards-compat: `rate_limit.provider_rpm` (the old key) is
    read when `limits.provider_rpm` is absent."""
    limits = Limits.from_config({"rate_limit": {"provider_rpm": 7}})
    assert limits.provider_rpm == 7


def test_limits_explicit_wins_over_legacy():
    cfg = {
        "limits": {"provider_rpm": 99},
        "rate_limit": {"provider_rpm": 7},
    }
    limits = Limits.from_config(cfg)
    assert limits.provider_rpm == 99


class _LoopProvider(LLMProvider):
    """Provider that always returns a tool call, so the loop never exits."""

    def __init__(self):
        super().__init__(name="loop", config={})
        self.call_count = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.call_count += 1
        return ChatResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"t{self.call_count}", name="echo", arguments="{}")
            ],
        )


@pytest.mark.asyncio
async def test_tool_loop_respects_max_tool_turns(tmp_path):
    from aaagent.core.app import Application

    cfg = _write_cfg(tmp_path, max_tool_turns=3)
    provider = _LoopProvider()
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)
    app = Application(
        config_path=str(cfg),
        bus=EventBus(),
        memory=memory,
        providers={"fake": provider},
        tool_registry=None,
    )
    app.set_provider(provider)

    sent: list[Message] = []

    async def handler(msg):
        sent.append(msg)

    bus = app._bus
    bus.on("message_to_send", handler)

    # Register a no-op echo tool so the loop has something to call
    async def _echo(args):
        return "ok"

    app._tool_registry.register(
        name="echo",
        description="noop",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_echo,
    )

    msg = Message(
        session_id="s", platform="cli", chat_id="c",
        user_id="u", content="hi", role="user",
    )
    await app._handle_message(msg)

    # 3 tool turns -> exactly 3 chat() calls (one per turn); the loop
    # terminates on the third turn with the "max turns" message.
    assert provider.call_count == 3
    assert any("最大工具调用次数" in m.content for m in sent)


class _SlowProvider(LLMProvider):
    """Provider that sleeps past the wall-clock cap."""

    def __init__(self, delay: float):
        super().__init__(name="slow", config={})
        self.delay = delay

    async def chat(self, messages, tools=None, **kwargs):
        await asyncio.sleep(self.delay)
        return ChatResponse(content="late")


@pytest.mark.asyncio
async def test_tool_loop_respects_wallclock(tmp_path):
    from aaagent.core.app import Application

    cfg = _write_cfg(tmp_path, max_tool_wallclock_s=0.05)
    provider = _SlowProvider(delay=1.0)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)
    app = Application(
        config_path=str(cfg),
        bus=EventBus(),
        memory=memory,
        providers={"fake": provider},
        tool_registry=None,
    )
    app.set_provider(provider)

    sent: list[Message] = []

    async def handler(msg):
        sent.append(msg)

    bus = app._bus
    bus.on("message_to_send", handler)

    msg = Message(
        session_id="s", platform="cli", chat_id="c",
        user_id="u", content="hi", role="user",
    )
    await app._handle_message(msg)

    # The wallclock cap fired before the slow provider returned;
    # the reply must mention "超时" (timeout) and must NOT be
    # the "late" string the provider would have produced.
    assert any("超时" in m.content for m in sent)
    assert not any("late" in m.content for m in sent)
