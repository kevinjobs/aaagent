from __future__ import annotations

import logging

import pytest

from aaagent.core.bus import EventBus
from aaagent.core.logctx import ContextFilter, reset_context, set_context
from aaagent.core.ratelimit import TokenBucket
from aaagent.providers.base import ChatResponse, LLMProvider, ToolCall


class _RecordingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(name="rec", config={})
        self.calls: list[tuple] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(("chat", list(messages), tools))
        return ChatResponse(content="hi", tool_calls=None)


@pytest.mark.asyncio
async def test_logctx_filter_injects_context_fields():
    f = ContextFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    assert f.filter(record) is True
    assert record.session_id == ""
    assert record.platform == ""

    tokens = set_context(session_id="s1", platform="feishu", chat_id="c1")
    try:
        record2 = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        f.filter(record2)
        assert record2.session_id == "s1"
        assert record2.platform == "feishu"
        assert record2.chat_id == "c1"
    finally:
        reset_context(tokens)

    record3 = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="x", args=(), exc_info=None,
    )
    f.filter(record3)
    assert record3.session_id == ""


@pytest.mark.asyncio
async def test_ratelimit_rejects_zero_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_min=0)


@pytest.mark.asyncio
async def test_ratelimit_acquire_consumes_token():
    bucket = TokenBucket(rate_per_min=60)
    await bucket.acquire()
    await bucket.acquire()
    assert bucket.tokens < bucket.capacity


@pytest.mark.asyncio
async def test_eventbus_handler_exception_does_not_break_bus():
    bus = EventBus()

    async def bad(_):
        raise RuntimeError("boom")

    received: list[str] = []

    async def good(_):
        received.append("ok")

    bus.on("evt", bad)
    bus.on("evt", good)
    await bus.emit("evt", None)
    assert received == ["ok"]


@pytest.mark.asyncio
async def test_eventbus_concurrent_handlers():
    import asyncio

    bus = EventBus()

    async def slow_a(_):
        await asyncio.sleep(0.05)

    async def slow_b(_):
        await asyncio.sleep(0.05)

    bus.on("evt", slow_a)
    bus.on("evt", slow_b)
    t0 = asyncio.get_event_loop().time()
    await bus.emit("evt", None)
    elapsed = asyncio.get_event_loop().time() - t0
    # If sequential, total would be ~100ms. Concurrent should be ~50ms.
    assert elapsed < 0.09, f"handlers did not run concurrently ({elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_message_to_llm_dict_tool_includes_name():
    from aaagent.core.message import Message

    m = Message(role="tool", tool_call_id="1", name="read_file", content="x")
    d = m.to_llm_dict()
    assert d["role"] == "tool"
    assert d["tool_call_id"] == "1"
    assert d["name"] == "read_file"
    assert d["content"] == "x"