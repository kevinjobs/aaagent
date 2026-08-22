"""Tests for the sqlitesession plugin (dual_write, sqlite, LLM tools)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from aaagent.core.bus import EventBus
from aaagent.core.logctx import current_user_id, set_context
from aaagent.core.message import Message
from aaagent.core.plugin import PluginManager
from aaagent.core.session import Session
from aaagent.core.tool_registry import ToolRegistry
from aaagent_plugin_inmemorysession import InMemorySessionStore
from aaagent_plugin_sqlitesession import (
    DualWriteSessionFactory,
    SqliteSessionFactory,
    SqliteSessionStore,
    SqliteSessionToolsPlugin,
    _DualWriteSessionStore,
)


def _make_msg(
    session_id: str = "s1",
    role: str = "user",
    content: str = "hi",
    platform: str = "feishu",
    chat_id: str = "chat-1",
    user_id: str = "u-1",
    ts: float | None = None,
) -> Message:
    return Message(
        session_id=session_id,
        role=role,
        content=content,
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
        timestamp=ts if ts is not None else time.time(),
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


@pytest.fixture
def tmp_db_path_str(tmp_db: Path) -> str:
    return str(tmp_db)


# ---------------------------------------------------------------------------
# SqliteSessionStore direct tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_message_persists_to_db(tmp_db_path_str: str, tmp_path: Path):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    msg = _make_msg(content="hello")
    await store.add_message("s1", msg)

    sess = await store._load_session("s1")
    assert sess is not None
    assert sess.platform == "feishu"
    assert sess.chat_id == "chat-1"
    assert len(sess.messages) == 1
    assert sess.messages[0].content == "hello"
    assert sess.last_activity == pytest.approx(msg.timestamp, abs=0.01)

    conn = await store._conn_open()
    cur = await conn.execute("SELECT user_id FROM sessions WHERE id = ?", ("s1",))
    row = await cur.fetchone()
    assert row[0] == "u-1"
    await store.close()


async def test_session_row_upsert_on_repeat_add(tmp_db_path_str: str, tmp_path: Path):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    msg1 = _make_msg(content="first", ts=100.0)
    msg2 = _make_msg(content="second", ts=200.0)
    await store.add_message("s1", msg1)
    await store.add_message("s1", msg2)

    sess = await store._load_session("s1")
    assert sess is not None
    assert sess.last_activity == 200.0
    assert [m.content for m in sess.messages] == ["first", "second"]
    await store.close()


async def test_get_or_create_creates_empty_session(tmp_db_path_str: str, tmp_path: Path):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    sess = await store.get_or_create("new", platform="cli", chat_id="c")
    assert sess.id == "new"
    assert sess.platform == "cli"
    assert sess.chat_id == "c"
    assert sess.messages == []
    await store.close()


async def test_drop_session_removes_messages(tmp_db_path_str: str, tmp_path: Path):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    await store.add_message("s1", _make_msg(content="x"))
    await store.drop_session("s1")
    sess = await store._load_session("s1")
    assert sess is None
    await store.close()


async def test_fetch_recent_sessions_orders_by_activity(
    tmp_db_path_str: str, tmp_path: Path
):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    await store.add_message("a", _make_msg(content="a1", session_id="a", ts=10.0))
    await store.add_message("b", _make_msg(content="b1", session_id="b", ts=20.0))
    await store.add_message("c", _make_msg(content="c1", session_id="c", ts=30.0))

    sessions = await store.fetch_recent_sessions(2)
    assert [s.id for s in sessions] == ["c", "b"]
    await store.close()


async def test_pragmas_wal_and_foreign_keys_applied(
    tmp_db_path_str: str, tmp_path: Path
):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    conn = await store._conn_open()
    cur = await conn.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row[0].lower() == "wal"
    cur = await conn.execute("PRAGMA foreign_keys")
    row = await cur.fetchone()
    assert row[0] == 1
    await store.close()


async def test_message_tool_calls_persisted_as_json(
    tmp_db_path_str: str, tmp_path: Path
):
    store = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    msg = Message(
        session_id="s1",
        role="assistant",
        content="",
        platform="feishu",
        chat_id="c",
        user_id="u",
        tool_calls=[{"id": "c1", "type": "function",
                     "function": {"name": "x", "arguments": "{}"}}],
    )
    await store.add_message("s1", msg)
    sess = await store._load_session("s1")
    assert sess is not None
    assert sess.messages[0].tool_calls is not None
    assert sess.messages[0].tool_calls[0]["id"] == "c1"
    await store.close()


# ---------------------------------------------------------------------------
# _DualWriteSessionStore tests
# ---------------------------------------------------------------------------


async def test_dual_write_writes_to_both_stores(tmp_db_path_str: str, tmp_path: Path):
    primary = InMemorySessionStore()
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)

    msg = _make_msg(content="dual")
    await store.add_message("s1", msg)

    sess_primary = await primary.get_session("s1")
    assert sess_primary.messages[0].content == "dual"

    await store._drain_pending()
    sess_secondary = await secondary._load_session("s1")
    assert sess_secondary is not None
    assert sess_secondary.messages[0].content == "dual"
    await store.close()


async def test_dual_write_exposes_system_prompt(tmp_db_path_str: str, tmp_path: Path):
    """Regression: app.py reads session_store._system_prompt; the
    dual-write wrapper must expose it (delegated to the primary)."""
    primary = InMemorySessionStore(system_prompt="hello dual")
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)
    assert store._system_prompt == "hello dual"
    await store.close()


async def test_dual_write_does_not_block_on_sqlite_failure(tmp_path: Path):
    primary = InMemorySessionStore()
    broken_path = tmp_path / "nope" / "x" / "y" / "sessions.db"
    secondary = SqliteSessionStore(db_path=str(broken_path), base_path=tmp_path)
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)

    msg = _make_msg(content="x")
    session = await store.add_message("s1", msg)
    assert session.messages[0].content == "x"

    sess_primary = await primary.get_session("s1")
    assert sess_primary.messages[0].content == "x"
    await store.close()


async def test_warmup_loads_last_n_into_primary(tmp_db_path_str: str, tmp_path: Path):
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    await secondary.add_message(
        "old", _make_msg(content="old1", session_id="old", ts=10.0)
    )
    await secondary.add_message(
        "mid", _make_msg(content="mid1", session_id="mid", ts=20.0)
    )
    await secondary.add_message(
        "new", _make_msg(content="new1", session_id="new", ts=30.0)
    )

    primary = InMemorySessionStore()
    store = _DualWriteSessionStore(primary, secondary, restore_n=1)
    await store.warmup()

    assert "new" in primary._sessions
    assert "mid" not in primary._sessions
    assert "old" not in primary._sessions
    await store.close()


async def test_lazy_load_on_miss(tmp_db_path_str: str, tmp_path: Path):
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    await secondary.add_message(
        "only", _make_msg(content="only-msg", session_id="only", ts=10.0)
    )
    primary = InMemorySessionStore()
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)
    await store.warmup()

    sess = await store.get_session("only")
    assert sess.id == "only"
    assert sess.messages[0].content == "only-msg"
    assert "only" in primary._sessions
    await store.close()


async def test_drop_session_removes_from_both(tmp_db_path_str: str, tmp_path: Path):
    primary = InMemorySessionStore()
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)
    await store.add_message("s1", _make_msg(content="x"))
    await store.drop_session("s1")
    await store._drain_pending()

    assert "s1" not in primary._sessions
    sess = await secondary._load_session("s1")
    assert sess is None
    await store.close()


async def test_close_drains_pending_writes(tmp_db_path_str: str, tmp_path: Path):
    primary = InMemorySessionStore()
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)
    for i in range(5):
        await store.add_message("s1", _make_msg(content=f"m{i}"))
    await store.close()

    sess = await secondary._load_session("s1")
    assert sess is not None
    assert len(sess.messages) == 5


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


async def test_factory_dual_write_creates_wrapper(tmp_db_path_str: str, tmp_path: Path):
    factory = DualWriteSessionFactory()
    config = {
        "max_history": 5,
        "compress_threshold": 0.8,
        "max_sessions": 100,
        "sqlite": {"db_path": tmp_db_path_str, "restore_n": 0,
                    "base_path": str(tmp_path)},
    }
    store = factory.create(config)
    assert isinstance(store, _DualWriteSessionStore)
    msg = _make_msg(content="factory")
    await store.add_message("s1", msg)
    sess_primary = await store._primary.get_session("s1")
    assert sess_primary.messages[0].content == "factory"
    await store.close()


async def test_factory_sqlite_creates_direct(tmp_db_path_str: str, tmp_path: Path):
    factory = SqliteSessionFactory()
    config = {
        "max_history": 5,
        "db_path": tmp_db_path_str,
        "base_path": str(tmp_path),
    }
    store = factory.create(config)
    assert isinstance(store, SqliteSessionStore)
    await store.add_message("s1", _make_msg(content="sqlite-direct"))
    sess = await store._load_session("s1")
    assert sess is not None
    assert sess.messages[0].content == "sqlite-direct"
    await store.close()


# ---------------------------------------------------------------------------
# LLM tools tests
# ---------------------------------------------------------------------------


class _FakeApp:
    def __init__(self, store: Any, last_platform: str = "feishu") -> None:
        self._session_store = store
        self._last_message = Message(
            session_id="x", role="user", content="",
            platform=last_platform, chat_id="c", user_id="",
        )


async def _make_app_with_store(
    tmp_db_path_str: str, tmp_path: Path, last_platform: str = "feishu"
) -> tuple[Any, SqliteSessionStore]:
    secondary = SqliteSessionStore(
        db_path=tmp_db_path_str, base_path=tmp_path
    )
    primary = InMemorySessionStore()
    store = _DualWriteSessionStore(primary, secondary, restore_n=0)
    app = _FakeApp(store, last_platform=last_platform)
    return app, secondary


def _build_registry(
    app: Any, config: dict[str, Any]
) -> ToolRegistry:
    plugin = SqliteSessionToolsPlugin()
    plugin.set_application(app)
    reg = ToolRegistry()
    plugin.register(reg, config)
    return reg


async def _run(reg: ToolRegistry, name: str, args: dict, user_id: str = "u-1") -> str:
    tok = set_context(user_id=user_id)
    try:
        return await reg.execute(name, json.dumps(args))
    finally:
        from aaagent.core.logctx import _user_id
        _user_id.set("")


async def test_session_search_filters_by_user_and_platform(
    tmp_db_path_str: str, tmp_path: Path
):
    app, store = await _make_app_with_store(tmp_db_path_str, tmp_path)
    await store.add_message(
        "s1", _make_msg(content="方案 1 alpha", session_id="s1",
                       user_id="u-1", platform="feishu")
    )
    await store.add_message(
        "s2", _make_msg(content="方案 1 beta", session_id="s2",
                       user_id="u-2", platform="feishu")
    )
    await store.add_message(
        "s3", _make_msg(content="方案 1 gamma", session_id="s3",
                       user_id="u-1", platform="cli")
    )

    config = {"tools": {"sqlite_session": {"enabled": True}}}
    reg = _build_registry(app, config)

    result = await _run(reg, "session_search", {"query": "方案 1"}, user_id="u-1")
    assert "session=s1" in result
    assert "session=s3" not in result
    assert "session=s2" not in result
    await store.close()


async def test_session_get_messages_owner_check(
    tmp_db_path_str: str, tmp_path: Path
):
    app, store = await _make_app_with_store(tmp_db_path_str, tmp_path)
    await store.add_message(
        "owned", _make_msg(content="mine", session_id="owned",
                            user_id="u-1", platform="feishu")
    )
    await store.add_message(
        "theirs", _make_msg(content="not-mine", session_id="theirs",
                            user_id="u-2", platform="feishu")
    )

    config = {"tools": {"sqlite_session": {"enabled": True}}}
    reg = _build_registry(app, config)

    own = await _run(reg, "session_get_messages", {"session_id": "owned"}, user_id="u-1")
    assert "mine" in own
    forbidden = await _run(
        reg, "session_get_messages", {"session_id": "theirs"}, user_id="u-1"
    )
    assert "not accessible" in forbidden.lower() or "未找到" in forbidden or "Error" in forbidden
    await store.close()


async def test_session_search_top_k_and_snippet_truncation(
    tmp_db_path_str: str, tmp_path: Path
):
    app, store = await _make_app_with_store(tmp_db_path_str, tmp_path)
    long = "x" * 500
    await store.add_message(
        "long", _make_msg(content=f"target {long}", session_id="long",
                          user_id="u-1", platform="feishu")
    )
    for i in range(15):
        await store.add_message(
            f"filler-{i}",
            _make_msg(content=f"target filler {i}", session_id=f"filler-{i}",
                      user_id="u-1", platform="feishu", ts=100.0 + i),
        )

    config = {"tools": {"sqlite_session": {"enabled": True}}}
    reg = _build_registry(app, config)

    result = await _run(
        reg, "session_search", {"query": "target", "top_k": 3}, user_id="u-1"
    )
    lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert len(lines) == 3
    raw_snippet = lines[0].split("snippet=", 1)[1]
    snippet_field = raw_snippet.strip("'\"")
    assert len(snippet_field) <= 200
    assert len(snippet_field) >= 100
    await store.close()


async def test_session_search_empty_when_no_match(
    tmp_db_path_str: str, tmp_path: Path
):
    app, store = await _make_app_with_store(tmp_db_path_str, tmp_path)
    await store.add_message(
        "s1", _make_msg(content="nothing here", session_id="s1",
                        user_id="u-1", platform="feishu")
    )
    config = {"tools": {"sqlite_session": {"enabled": True}}}
    reg = _build_registry(app, config)

    result = await _run(reg, "session_search", {"query": "xyz-no-match"}, user_id="u-1")
    assert "没有找到" in result or "not found" in result.lower()
    await store.close()


async def test_tools_disabled_when_config_says_so(
    tmp_db_path_str: str, tmp_path: Path
):
    app, store = await _make_app_with_store(tmp_db_path_str, tmp_path)
    config = {"tools": {"sqlite_session": {"enabled": False}}}
    reg = _build_registry(app, config)
    assert "session_search" not in reg.tool_names
    assert "session_get_messages" not in reg.tool_names
    await store.close()


async def test_tools_skipped_when_no_sqlite_store(tmp_path: Path):
    primary = InMemorySessionStore()
    app = _FakeApp(primary)
    config = {"tools": {"sqlite_session": {"enabled": True}}}
    reg = _build_registry(app, config)
    assert "session_search" not in reg.tool_names
    assert "session_get_messages" not in reg.tool_names
