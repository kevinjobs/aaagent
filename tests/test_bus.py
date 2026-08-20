import pytest

from aaagent.core.bus import EventBus


@pytest.mark.asyncio
async def test_emit_calls_handler():
    bus = EventBus()
    received = []

    async def handler(data):
        received.append(data)

    bus.on("test_event", handler)
    await bus.emit("test_event", {"x": 1})
    await bus.emit("test_event", {"x": 2})
    assert received == [{"x": 1}, {"x": 2}]


@pytest.mark.asyncio
async def test_multiple_handlers_called_in_order():
    bus = EventBus()
    order = []

    async def h1(data):
        order.append("h1")

    async def h2(data):
        order.append("h2")

    bus.on("evt", h1)
    bus.on("evt", h2)
    await bus.emit("evt")
    assert order == ["h1", "h2"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_others():
    bus = EventBus()
    seen = []

    async def bad(data):
        raise RuntimeError("boom")

    async def good(data):
        seen.append(data)

    bus.on("evt", bad)
    bus.on("evt", good)
    await bus.emit("evt", "payload")
    assert seen == ["payload"]


@pytest.mark.asyncio
async def test_emit_with_no_handlers_is_noop():
    bus = EventBus()
    await bus.emit("nope")  # should not raise


@pytest.mark.asyncio
async def test_emit_passes_none_by_default():
    bus = EventBus()
    received = []

    async def handler(data):
        received.append(data)

    bus.on("evt", handler)
    await bus.emit("evt")
    assert received == [None]