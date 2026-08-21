from __future__ import annotations

import pytest

from aaagent.core.app import Application
from aaagent.core.bus import EventBus
from aaagent.core.message import Message
from aaagent.core.types import ChatResponse, LLMProvider, ToolCall
from aaagent_plugin_markdownstore import MarkdownMemoryStore


def _write_minimal_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "default_provider: fake\n"
        "providers:\n"
        "  fake:\n"
        "    type: custom\n"
        "    class: tests.conftest.FakeProvider\n"
        "    enabled: true\n"
        "tools:\n"
        "  shell:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    return cfg


@pytest.mark.asyncio
async def test_application_with_injected_fakes_runs(tmp_path):
    """End-to-end: a message goes through bus -> session -> provider -> reply."""
    cfg = _write_minimal_config(tmp_path)
    bus = EventBus()
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    class _P(LLMProvider):
        def __init__(self):
            super().__init__(name="p", config={})

        async def chat(self, messages, tools=None, **kwargs):
            return ChatResponse(content="reply text")

    provider = _P()
    app = Application(
        config_path=str(cfg),
        bus=bus,
        memory=memory,
        providers={"fake": provider},
    )
    app.set_provider(provider)

    sent: list[Message] = []
    bus.on("message_to_send", lambda m: sent.append(m) or asyncio.create_task(sent_ack(sent)))

    import asyncio

    async def sent_ack(_):
        pass

    # Replace the handler with an async one
    sent.clear()

    async def handler(msg):
        sent.append(msg)

    bus.on("message_to_send", handler)
    # remove the lambda we added for compatibility
    bus._handlers["message_to_send"].pop(0)

    msg = Message(
        session_id="s1",
        platform="cli",
        chat_id="c1",
        user_id="u1",
        content="hello",
        role="user",
    )
    await app._handle_message(msg)

    assert len(sent) == 1
    assert sent[0].content == "reply text"
    assert sent[0].role == "assistant"


@pytest.mark.asyncio
async def test_application_tool_loop_length_aborts(tmp_path):
    cfg = _write_minimal_config(tmp_path)
    bus = EventBus()
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    class _BigProvider(LLMProvider):
        def __init__(self):
            super().__init__(name="big", config={})
            self.call_count = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.call_count += 1
            return ChatResponse(content="x")

    provider = _BigProvider()
    app = Application(
        config_path=str(cfg),
        bus=bus,
        memory=memory,
        providers={"fake": provider},
    )
    app.set_provider(provider)

    big = "a" * 50000
    for _ in range(5):
        await app._session_store.add_message(
            "s1",
            Message(
                session_id="s1",
                platform="cli",
                chat_id="c1",
                user_id="u1",
                content=big,
                role="user",
            ),
        )

    sent: list[Message] = []

    async def handler(msg):
        sent.append(msg)

    bus.on("message_to_send", handler)

    msg = Message(
        session_id="s1",
        platform="cli",
        chat_id="c1",
        user_id="u1",
        content="hello",
        role="user",
    )
    await app._handle_message(msg)

    # Length guard should have aborted before provider.chat was called
    assert provider.call_count == 0
    assert len(sent) == 1
    assert "上下文过长" in sent[0].content


@pytest.mark.asyncio
async def test_archive_idle_sessions_sweep(tmp_path):
    """Idle sessions are archived to memory and dropped from the store."""
    import time

    cfg = _write_minimal_config(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)
    app = Application(
        config_path=str(cfg),
        bus=EventBus(),
        memory=memory,
        providers={},
    )
    app._archive_interval = 1  # seconds, for testability

    await app._session_store.add_message(
        "stale1",
        Message(
            session_id="stale1",
            platform="cli",
            chat_id="c",
            user_id="u1",
            content="hello",
            role="user",
        ),
    )
    await app._session_store.add_message(
        "fresh1",
        Message(
            session_id="fresh1",
            platform="cli",
            chat_id="c",
            user_id="u1",
            content="hi",
            role="user",
        ),
    )
    stale = await app._session_store.get_session("stale1")
    stale.last_activity = time.time() - 7200

    await app._archive_idle_sessions()

    remaining = {s.id for s in app._session_store.list_sessions()}
    assert "stale1" not in remaining
    assert "fresh1" in remaining

    archive = memory._archive_path.read_text(encoding="utf-8")
    assert "## Session: stale1" in archive
    assert "fresh1" not in archive