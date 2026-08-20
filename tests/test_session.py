import pytest

from aaagent.core.message import Message
from aaagent.core.session import Session, SessionStore


class FakeProvider:
    """Stub LLM provider that records calls and returns canned summaries."""

    def __init__(self, summary: str = "summary") -> None:
        self.summary = summary
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.summary


@pytest.mark.asyncio
async def test_add_message_increments_session():
    store = SessionStore()
    msg = Message(session_id="s1", role="user", content="hi")
    await store.add_message("s1", msg)
    session = store.get_or_create("s1")
    assert len(session.messages) == 1
    assert session.messages[0].content == "hi"


@pytest.mark.asyncio
async def test_get_context_no_summary():
    store = SessionStore()
    msg = Message(role="user", content="hello")
    await store.add_message("s1", msg)
    ctx = await store.get_context("s1")
    assert ctx == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_get_context_with_summary_prepended():
    session = Session(id="s1", summary="past conversation summary")
    session.messages.append(Message(role="user", content="now"))
    ctx = session.get_context()
    assert ctx[0] == {"role": "system", "content": "对话历史摘要：past conversation summary"}
    assert ctx[1] == {"role": "user", "content": "now"}


@pytest.mark.asyncio
async def test_needs_compress_only_when_over_max():
    session = Session(id="s1", max_history=3)
    assert not session.needs_compress()
    for i in range(3):
        session.messages.append(Message(content=str(i)))
    assert not session.needs_compress()
    session.messages.append(Message(content="overflow"))
    assert session.needs_compress()


@pytest.mark.asyncio
async def test_compress_trims_to_keep_after_threshold():
    provider = FakeProvider(summary="brief")
    session = Session(id="s1", max_history=10, compress_threshold=0.5)
    for i in range(12):
        session.messages.append(Message(role="user" if i % 2 == 0 else "assistant", content=f"m{i}"))
    assert session.needs_compress()
    await session.compress(provider)
    assert len(session.messages) == 5
    assert session.summary == "brief"
    assert provider.calls  # LLM was called once


@pytest.mark.asyncio
async def test_compress_idempotent_when_under_cap():
    provider = FakeProvider()
    session = Session(id="s1", max_history=20)
    for i in range(5):
        session.messages.append(Message(content=str(i)))
    await session.compress(provider)
    assert len(session.messages) == 5
    assert provider.calls == []  # no LLM call needed


@pytest.mark.asyncio
async def test_compress_includes_previous_summary():
    provider = FakeProvider(summary="updated")
    session = Session(id="s1", max_history=4, compress_threshold=0.5)
    session.summary = "previous summary"
    for i in range(6):
        session.messages.append(Message(content=f"m{i}"))
    await session.compress(provider)
    prompt = provider.calls[0][0]["content"]
    assert "previous summary" in prompt


@pytest.mark.asyncio
async def test_concurrent_add_is_serialized():
    import asyncio

    store = SessionStore()
    msgs = [Message(content=str(i)) for i in range(50)]

    async def add(m):
        await store.add_message("s", m)

    await asyncio.gather(*(add(m) for m in msgs))
    session = store.get_or_create("s")
    assert len(session.messages) == 50


@pytest.mark.asyncio
async def test_lru_eviction_when_over_max_sessions():
    store = SessionStore(max_sessions=3)
    for i in range(5):
        await store.add_message(f"s{i}", Message(content=str(i)))
    sessions = store.list_sessions()
    assert len(sessions) == 3
    # Oldest two should be evicted
    ids = {s.id for s in sessions}
    assert "s3" in ids and "s4" in ids
    assert "s0" not in ids and "s1" not in ids


@pytest.mark.asyncio
async def test_max_sessions_property():
    store = SessionStore(max_sessions=42)
    assert store.max_sessions == 42