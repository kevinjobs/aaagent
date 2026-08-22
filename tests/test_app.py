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
        "providers:\n"
        "  _meta:\n"
        "    default: fake\n"
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


class _RaisingProvider(LLMProvider):
    """Test provider whose `chat` raises whatever is queued (or returns OK)."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, config={})
        self.queue: list[Exception | ChatResponse] = []
        self.calls: list[tuple[list, object]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append((list(messages), tools))
        if not self.queue:
            return ChatResponse(content=f"ok-{self.name}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fallback_cfg(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "providers:\n"
        "  _meta:\n"
        "    default: primary\n"
        "    fallback:\n"
        "      - backup\n"
        "      - absent\n",
        encoding="utf-8",
    )
    return str(cfg)


def test_missing_config_auto_copies_example(tmp_path):
    """First run: config.yaml absent -> _load_config copies example."""
    example = tmp_path / "config.yaml.example"
    example.write_text("providers:\n  _meta:\n    default: fake\n", encoding="utf-8")

    cfg = tmp_path / "config.yaml"
    app = Application(config_path=str(cfg), enabled_adapters=[])
    assert cfg.exists()
    assert cfg.read_text(encoding="utf-8") == example.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_chat_with_fallback_switches_provider(tmp_path):
    cfg = _fallback_cfg(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    primary = _RaisingProvider("primary")
    primary.queue.append(ConnectionError("connection reset"))
    backup = _RaisingProvider("backup")

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={"primary": primary, "backup": backup},
    )
    result = await app._chat_with_fallback([{"role": "user", "content": "hi"}])

    assert result.content == "ok-backup"
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1


@pytest.mark.asyncio
async def test_chat_with_fallback_raises_non_retryable(tmp_path):
    cfg = _fallback_cfg(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    primary = _RaisingProvider("primary")
    primary.queue.append(ValueError("bad request"))
    backup = _RaisingProvider("backup")

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={"primary": primary, "backup": backup},
    )
    with pytest.raises(ValueError):
        await app._chat_with_fallback([{"role": "user", "content": "hi"}])
    assert len(backup.calls) == 0


@pytest.mark.asyncio
async def test_chat_with_fallback_all_fail_raises(tmp_path):
    cfg = _fallback_cfg(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    primary = _RaisingProvider("primary")
    primary.queue.append(ConnectionError("boom1"))
    backup = _RaisingProvider("backup")
    backup.queue.append(ConnectionError("boom2"))

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={"primary": primary, "backup": backup},
    )
    with pytest.raises(ConnectionError):
        await app._chat_with_fallback([{"role": "user", "content": "hi"}])


def test_resolve_provider_chain_order(tmp_path):
    cfg = _fallback_cfg(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={
            "primary": _RaisingProvider("primary"),
            "backup": _RaisingProvider("backup"),
            "other": _RaisingProvider("other"),
        },
    )
    assert [p.name for p in app._provider_order] == ["primary", "backup"]


@pytest.mark.asyncio
async def test_stream_or_chat_falls_back_before_first_chunk(tmp_path):
    cfg = _fallback_cfg(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    class _BadStream(_RaisingProvider):
        async def stream_chat(self, messages, **kwargs):
            raise ConnectionError("streaming broke")
            yield  # pragma: no cover

    primary = _BadStream("primary")
    backup = _RaisingProvider("backup")

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={"primary": primary, "backup": backup},
    )
    result = await app._stream_or_chat([{"role": "user", "content": "hi"}])
    assert result == "ok-backup"


def test_is_retryable_moderation_minimax():
    """MiniMax returns 422 'input new_sensitive (1026)' — should fall back."""
    from aaagent.core.app import _is_retryable_provider_error

    err = Exception("Error code: 422 - input new_sensitive (1026)")
    assert _is_retryable_provider_error(err) is True


def test_is_retryable_unprocessable_entity_generic():
    from aaagent.core.app import _is_retryable_provider_error

    err = Exception("Error code: 422 unprocessable_entity")
    assert _is_retryable_provider_error(err) is True


def test_is_retryable_azure_content_filter():
    from aaagent.core.app import _is_retryable_provider_error

    err = Exception("content_policy_violation triggered")
    assert _is_retryable_provider_error(err) is True


def test_not_retryable_bare_validation_error():
    """Non-retryable errors must still bubble immediately."""
    from aaagent.core.app import _is_retryable_provider_error

    err = ValueError("bad input shape")
    assert _is_retryable_provider_error(err) is False


@pytest.mark.asyncio
async def test_moderation_block_triggers_fallback(tmp_path):
    """End-to-end: primary provider's moderation block falls through to backup."""
    from aaagent.core.app import Application
    from aaagent.core.bus import EventBus

    cfg = _write_minimal_config(tmp_path)
    memory = MarkdownMemoryStore(data_dir="data", base_path=tmp_path)

    class _ModerationProvider(LLMProvider):
        async def chat(self, messages, tools=None, **kwargs):
            raise Exception("Error code: 422 - input new_sensitive (1026)")

    class _OkProvider(LLMProvider):
        async def chat(self, messages, tools=None, **kwargs):
            return ChatResponse(content="ok-backup")

    primary = _ModerationProvider.__new__(_ModerationProvider)
    primary.__init__(name="primary", config={})
    backup = _OkProvider.__new__(_OkProvider)
    backup.__init__(name="backup", config={})

    app = Application(
        config_path=cfg,
        bus=EventBus(),
        memory=memory,
        providers={"primary": primary, "backup": backup},
        enabled_adapters=[],
    )
    # Wire the provider order explicitly: primary fails with moderation,
    # backup succeeds.
    app._provider_order = [primary, backup]
    result = await app._chat_with_fallback([{"role": "user", "content": "hi"}])
    assert result.content == "ok-backup"