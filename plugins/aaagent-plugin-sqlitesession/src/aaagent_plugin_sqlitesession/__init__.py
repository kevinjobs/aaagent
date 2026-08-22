"""SQLite-backed dual-write session store and history search tools.

Wired via entry-points:

1. Session factories (``aaagent.sessions``):
   - ``dual_write``: wraps an :class:`InMemorySessionStore` as the hot
     path and asynchronously mirrors every write to a SQLite database.
     On startup the most-recently-active N sessions are rehydrated so
     the user can resume immediately after a restart; other sessions
     are lazy-loaded on first access.
   - ``sqlite``: a SessionStore that talks directly to SQLite with no
     in-memory cache. Useful for debugging.

2. LLM tools (``aaagent.tools``):
   - :func:`session_search`: keyword search across past messages,
     scoped to ``user_id == current_user_id() AND platform ==
     current_platform``.
   - :func:`session_get_messages`: dump the message list of a single
     session with the same scope check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from aaagent.core.logctx import current_user_id
from aaagent.core.message import Message
from aaagent.core.plugin import SessionStoreFactory, ToolPlugin
from aaagent.core.session import Session, SessionStore

if TYPE_CHECKING:
    from aaagent.core.tool_registry import ToolRegistry

logger = logging.getLogger("aaagent.plugins.sqlitesession")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    summary       TEXT,
    system_prompt TEXT,
    created_at    REAL NOT NULL,
    last_activity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_platform
    ON sessions (user_id, platform, last_activity DESC);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    content       TEXT,
    raw           TEXT,
    timestamp     REAL NOT NULL,
    tool_call_id  TEXT,
    name          TEXT,
    tool_calls    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session_ts
    ON messages (session_id, timestamp ASC);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)

_DEFAULT_DB_PATH = "data/sessions.db"
_SNIPPET_LIMIT = 200


def _is_tool_message(m: Message) -> bool:
    return m.role == "tool" or (m.role == "assistant" and bool(m.tool_calls))


def _msg_to_row(msg: Message) -> tuple:
    return (
        msg.id,
        msg.session_id,
        msg.role,
        msg.content,
        json.dumps(msg.raw, ensure_ascii=False) if msg.raw is not None else None,
        msg.timestamp,
        msg.tool_call_id or None,
        msg.name or None,
        json.dumps(msg.tool_calls, ensure_ascii=False) if msg.tool_calls else None,
    )


def _row_to_msg(row: Any) -> Message:
    raw = None
    if row[4]:
        try:
            raw = json.loads(row[4])
        except (TypeError, ValueError):
            raw = None
    tool_calls = None
    if row[8]:
        try:
            tool_calls = json.loads(row[8])
        except (TypeError, ValueError):
            tool_calls = None
    return Message(
        id=row[0],
        session_id=row[1],
        role=row[2],
        content=row[3] or "",
        raw=raw,
        timestamp=row[5],
        tool_call_id=row[6] or "",
        name=row[7] or "",
        tool_calls=tool_calls,
    )


def _resolve_db_path(db_path: str, base_path: Path | None) -> Path:
    raw = Path(db_path)
    if raw.is_absolute():
        return raw
    base = Path(base_path).resolve() if base_path else Path.cwd()
    return base / raw


class SqliteSessionStore(SessionStore):
    """Direct SQLite-backed session store (no in-memory cache).

    Every ``add_message`` is a real disk write. Compression (when
    ``provider`` is supplied) follows the same rules as the in-memory
    store: once ``len(messages) > max_history``, the oldest messages
    are summarised and the originals removed.
    """

    def __init__(
        self,
        db_path: str = _DEFAULT_DB_PATH,
        max_history: int = 20,
        compress_threshold: float = 0.8,
        max_sessions: int = 1000,
        system_prompt: str = "",
        base_path: Path | None = None,
    ) -> None:
        super().__init__(
            max_history=max_history,
            compress_threshold=compress_threshold,
            max_sessions=max_sessions,
            system_prompt=system_prompt,
        )
        self._db_path = _resolve_db_path(db_path, base_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def _conn_open(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            for pragma in _PRAGMAS:
                await self._conn.execute(pragma)
            await self._conn.executescript(_SCHEMA)
            await self._conn.commit()
        return self._conn

    async def _load_session(self, session_id: str) -> Session | None:
        conn = await self._conn_open()
        async with conn.execute(
            "SELECT id, platform, chat_id, summary, system_prompt, "
            "created_at, last_activity FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        async with conn.execute(
            "SELECT id, session_id, role, content, raw, timestamp, "
            "tool_call_id, name, tool_calls FROM messages "
            "WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        ) as cur:
            msgs = await cur.fetchall()
        return Session(
            id=row[0],
            platform=row[1],
            chat_id=row[2],
            messages=[_row_to_msg(r) for r in msgs],
            summary=row[3],
            max_history=self._max_history,
            compress_threshold=self._compress_threshold,
            created_at=row[5],
            last_activity=row[6],
            system_prompt=row[4] or self._system_prompt,
        )

    async def fetch_recent_sessions(self, n: int) -> list[Session]:
        conn = await self._conn_open()
        async with conn.execute(
            "SELECT id FROM sessions ORDER BY last_activity DESC LIMIT ?",
            (int(n),),
        ) as cur:
            rows = await cur.fetchall()
        out: list[Session] = []
        for r in rows:
            sess = await self._load_session(r[0])
            if sess is not None:
                out.append(sess)
        return out

    async def get_or_create(
        self, session_id: str, platform: str = "", chat_id: str = ""
    ) -> Session:
        sess = await self._load_session(session_id)
        if sess is not None:
            return sess
        return Session(
            id=session_id,
            platform=platform,
            chat_id=chat_id,
            max_history=self._max_history,
            compress_threshold=self._compress_threshold,
            system_prompt=self._system_prompt,
        )

    async def add_message(
        self,
        session_id: str,
        msg: Message,
        provider: Any = None,
    ) -> Session:
        async with self._get_lock(session_id):
            conn = await self._conn_open()
            await conn.execute(
                "INSERT INTO sessions "
                "(id, platform, chat_id, user_id, created_at, last_activity) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "platform = excluded.platform, "
                "chat_id = excluded.chat_id, "
                "user_id = excluded.user_id, "
                "last_activity = excluded.last_activity",
                (
                    session_id,
                    msg.platform,
                    msg.chat_id,
                    msg.user_id or "",
                    msg.timestamp,
                    msg.timestamp,
                ),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO messages "
                "(id, session_id, role, content, raw, timestamp, "
                "tool_call_id, name, tool_calls) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _msg_to_row(msg),
            )
            await conn.commit()
            session = await self._load_session(session_id)
            assert session is not None
            if provider is not None and session.needs_compress():
                await self._compress(session_id, session, provider)
            return session

    async def _compress(
        self, session_id: str, session: Session, provider: Any
    ) -> None:
        keep = session.keep_after_compress
        old = session.messages[: len(session.messages) - keep]
        if not old:
            return
        text_messages = [m for m in old if not _is_tool_message(m)]
        if not text_messages:
            session.messages = session.messages[len(old):]
            session.last_activity = time.time()
            await self._truncate(session_id, [m.id for m in session.messages])
            return
        conversation = "\n".join(
            f"{m.role}: {m.content}" for m in text_messages
        )
        existing = (
            f"之前的对话摘要：{session.summary}\n\n" if session.summary else ""
        )
        prompt = (
            f"{existing}请将以下对话历史总结为一段简洁的摘要，"
            f"保留关键信息和上下文：\n\n{conversation}"
        )
        session.summary = await provider.chat(
            [{"role": "user", "content": prompt}]
        )
        session.messages = session.messages[len(old):]
        session.last_activity = time.time()
        conn = await self._conn_open()
        await conn.execute(
            "UPDATE sessions SET summary = ?, last_activity = ? WHERE id = ?",
            (session.summary, session.last_activity, session_id),
        )
        await conn.commit()
        await self._truncate(session_id, [m.id for m in session.messages])

    async def _truncate(self, session_id: str, keep_ids: list[str]) -> None:
        if not keep_ids:
            conn = await self._conn_open()
            await conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            await conn.commit()
            return
        placeholders = ",".join("?" for _ in keep_ids)
        conn = await self._conn_open()
        await conn.execute(
            f"DELETE FROM messages WHERE session_id = ? "
            f"AND id NOT IN ({placeholders})",
            (session_id, *keep_ids),
        )
        await conn.commit()

    async def get_context(self, session_id: str) -> list[dict[str, str]]:
        async with self._get_lock(session_id):
            session = await self.get_or_create(session_id)
            return session.get_context()

    async def get_session(self, session_id: str) -> Session:
        async with self._get_lock(session_id):
            return await self.get_or_create(session_id)

    async def drop_session(self, session_id: str) -> None:
        conn = await self._conn_open()
        await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await conn.commit()

    def list_sessions(self) -> list[Session]:
        return []

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class _DualWriteSessionStore(SessionStore):
    """In-memory primary + async SQLite mirror.

    The primary store handles every read and write synchronously so the
    hot path is unchanged. SQLite writes are serialised with an
    asyncio lock and scheduled via :func:`asyncio.create_task` so a
    SQLite failure never breaks a conversation. On first access to a
    ``session_id`` that the primary has never seen, the session is
    lazy-loaded from SQLite into the primary.
    """

    def __init__(
        self,
        primary: SessionStore,
        secondary: SqliteSessionStore,
        restore_n: int = 1,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._restore_n = max(0, int(restore_n))
        self._pending: set[asyncio.Task[Any]] = set()
        self._pending_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def warmup(self) -> None:
        if self._restore_n <= 0:
            return
        sessions = await self._secondary.fetch_recent_sessions(self._restore_n)
        primary_sessions = getattr(self._primary, "_sessions", None)
        primary_locks = getattr(self._primary, "_locks", None)
        if isinstance(primary_sessions, dict):
            for sess in sessions:
                primary_sessions[sess.id] = sess
                if isinstance(primary_locks, dict):
                    primary_locks.setdefault(sess.id, asyncio.Lock())
        logger.info("Warmup loaded %d session(s) from SQLite", len(sessions))

    async def _enqueue_write(self, coro_factory: Any) -> None:
        async def _runner() -> None:
            try:
                async with self._write_lock:
                    await coro_factory()
            except Exception as e:  # noqa: BLE001
                logger.warning("SQLite mirror write failed: %s", e)
            finally:
                async with self._pending_lock:
                    self._pending.discard(task)

        task = asyncio.create_task(_runner())
        async with self._pending_lock:
            self._pending.add(task)

    def _has_in_primary(self, session_id: str) -> bool:
        primary_sessions = getattr(self._primary, "_sessions", None)
        if isinstance(primary_sessions, dict):
            return session_id in primary_sessions
        return True

    def _inject_into_primary(self, session: Session) -> None:
        primary_sessions = getattr(self._primary, "_sessions", None)
        primary_locks = getattr(self._primary, "_locks", None)
        if isinstance(primary_sessions, dict):
            primary_sessions[session.id] = session
            if isinstance(primary_locks, dict):
                primary_locks.setdefault(session.id, asyncio.Lock())

    async def add_message(
        self,
        session_id: str,
        msg: Message,
        provider: Any = None,
    ) -> Session:
        session = await self._primary.add_message(
            session_id, msg, provider=provider
        )
        await self._enqueue_write(
            lambda: self._secondary.add_message(
                session_id, msg, provider=provider
            )
        )
        return session

    async def _ensure_loaded(self, session_id: str) -> None:
        if self._has_in_primary(session_id):
            return
        loaded = await self._secondary._load_session(session_id)
        if loaded is not None:
            self._inject_into_primary(loaded)

    async def get_session(self, session_id: str) -> Session:
        await self._ensure_loaded(session_id)
        return await self._primary.get_session(session_id)

    async def get_context(self, session_id: str) -> list[dict[str, str]]:
        await self._ensure_loaded(session_id)
        return await self._primary.get_context(session_id)

    async def drop_session(self, session_id: str) -> None:
        await self._primary.drop_session(session_id)
        await self._enqueue_write(
            lambda: self._secondary.drop_session(session_id)
        )

    def list_sessions(self) -> list[Session]:
        return self._primary.list_sessions()

    @property
    def max_sessions(self) -> int:
        return self._primary.max_sessions

    async def close(self) -> None:
        await self._drain_pending()
        await self._secondary.close()

    async def _drain_pending(self) -> None:
        if not self._pending:
            return
        pending = list(self._pending)
        await asyncio.gather(*pending, return_exceptions=True)


class DualWriteSessionFactory(SessionStoreFactory):
    """Factory producing :class:`_DualWriteSessionStore`."""

    name = "dual_write"

    def create(self, config: dict[str, Any]) -> SessionStore:
        from aaagent_plugin_inmemorysession import InMemorySessionStore

        primary = InMemorySessionStore(
            max_history=int(config.get("max_history", 20)),
            compress_threshold=float(config.get("compress_threshold", 0.8)),
            max_sessions=int(config.get("max_sessions", 1000)),
            system_prompt=str(config.get("system_prompt", "")),
        )
        sqlite_cfg = config.get("sqlite", {}) or {}
        secondary = SqliteSessionStore(
            db_path=str(sqlite_cfg.get("db_path", _DEFAULT_DB_PATH)),
            max_history=int(config.get("max_history", 20)),
            compress_threshold=float(config.get("compress_threshold", 0.8)),
            max_sessions=int(config.get("max_sessions", 1000)),
            system_prompt=str(config.get("system_prompt", "")),
            base_path=(
                Path(sqlite_cfg["base_path"]).resolve()
                if sqlite_cfg.get("base_path")
                else None
            ),
        )
        restore_n = int(sqlite_cfg.get("restore_n", 1))
        store = _DualWriteSessionStore(
            primary=primary, secondary=secondary, restore_n=restore_n
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(store.warmup())
        except RuntimeError:
            pass
        return store


class SqliteSessionFactory(SessionStoreFactory):
    """Factory producing :class:`SqliteSessionStore` directly."""

    name = "sqlite"

    def create(self, config: dict[str, Any]) -> SessionStore:
        return SqliteSessionStore(
            db_path=str(config.get("db_path", _DEFAULT_DB_PATH)),
            max_history=int(config.get("max_history", 20)),
            compress_threshold=float(config.get("compress_threshold", 0.8)),
            max_sessions=int(config.get("max_sessions", 1000)),
            system_prompt=str(config.get("system_prompt", "")),
            base_path=(
                Path(config["base_path"]).resolve()
                if config.get("base_path")
                else None
            ),
        )


class SqliteSessionToolsPlugin(ToolPlugin):
    """LLM tools for searching and reading past sessions.

    Both tools scope by ``current_user_id()`` and the current
    ``platform``; cross-user reads silently return empty rather than
    raising, so the LLM cannot probe for other users' sessions.
    """

    name = "sqlite_session"
    uses_memory_store = False

    def __init__(self) -> None:
        self._store: SqliteSessionStore | None = None
        self._app: Any = None

    def set_application(self, app: Any) -> None:
        self._app = app
        store = getattr(app, "_session_store", None)
        if isinstance(store, _DualWriteSessionStore):
            self._store = store._secondary
        elif isinstance(store, SqliteSessionStore):
            self._store = store

    def register(self, registry: ToolRegistry, config: dict[str, Any]) -> None:
        cfg = (config.get("tools", {}) or {}).get("sqlite_session", {}) or {}
        if not cfg.get("enabled", True):
            return
        if self._store is None:
            logger.debug(
                "sqlite_session tools not registered: no SQLite store bound"
            )
            return

        store = self._store

        async def _session_search(args: dict[str, Any]) -> str:
            query = str(args.get("query", "")).strip()
            if not query:
                return "Error: query is required"
            top_k = int(args.get("top_k", 10) or 10)
            since = args.get("since")
            caller_user = current_user_id() or ""
            caller_platform = _detect_platform(self._app)

            sql = (
                "SELECT m.session_id, m.role, m.content, m.timestamp, "
                "s.platform, s.user_id "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.user_id = ? AND s.platform = ? "
                "AND m.content LIKE ?"
            )
            params: list[Any] = [caller_user, caller_platform, f"%{query}%"]
            if since:
                sql += " AND m.timestamp >= ?"
                params.append(float(since))
            sql += " ORDER BY m.timestamp DESC LIMIT ?"
            params.append(top_k)

            conn = await store._conn_open()
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()

            if not rows:
                return "没有找到匹配的会话记录。"
            lines = []
            for r in rows:
                raw = r[2] or ""
                snippet = raw[:_SNIPPET_LIMIT]
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3]))
                lines.append(
                    f"- [{ts}] session={r[0]} role={r[1]} "
                    f"snippet={snippet!r}"
                )
            return "\n".join(lines)

        async def _session_get_messages(args: dict[str, Any]) -> str:
            session_id = str(args.get("session_id", "")).strip()
            if not session_id:
                return "Error: session_id is required"
            limit = int(args.get("limit", 50) or 50)
            limit = max(1, min(limit, 200))
            since_ts = args.get("since_timestamp")
            caller_user = current_user_id() or ""
            caller_platform = _detect_platform(self._app)

            conn = await store._conn_open()
            async with conn.execute(
                "SELECT user_id, platform FROM sessions WHERE id = ?",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return "Error: session not found"
            if row[0] != caller_user or row[1] != caller_platform:
                return "Error: session not accessible"

            sql = (
                "SELECT id, role, content, timestamp FROM messages "
                "WHERE session_id = ?"
            )
            params: list[Any] = [session_id]
            if since_ts is not None:
                sql += " AND timestamp >= ?"
                params.append(float(since_ts))
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)

            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
            if not rows:
                return "(empty)"
            lines = []
            for r in rows:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3]))
                lines.append(f"- [{ts}] {r[1]}: {r[2] or ''}")
            return "\n".join(lines)

        registry.register(
            name="session_search",
            description=(
                "搜索历史会话中包含关键词的消息。仅返回当前用户、当前平台"
                "（feishu/cli/...）的会话。用法：传入 query（必填）、top_k、"
                "since (Unix 时间戳)。返回命中片段、session_id、时间戳。"
                "无法访问其他用户或跨平台的会话。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（必填）。",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "最多返回的条数，默认 10。",
                    },
                    "since": {
                        "type": "number",
                        "description": "Unix 时间戳，仅返回此时间之后的命中。",
                    },
                },
                "required": ["query"],
            },
            handler=_session_search,
        )
        registry.register(
            name="session_get_messages",
            description=(
                "获取某个历史会话的消息列表。仅当该会话属于当前用户、"
                "当前平台时才返回；否则拒绝。用法：session_id（必填）、"
                "limit（默认 50，最大 200）、since_timestamp。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "目标会话 ID（必填）。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回的消息条数，默认 50，最大 200。",
                    },
                    "since_timestamp": {
                        "type": "number",
                        "description": "Unix 时间戳，仅返回此时间之后的消息。",
                    },
                },
                "required": ["session_id"],
            },
            handler=_session_get_messages,
        )


def _detect_platform(app: Any) -> str:
    last = getattr(app, "_last_message", None) if app is not None else None
    if last is not None and getattr(last, "platform", None):
        return str(last.platform)
    return ""


__all__ = [
    "SqliteSessionStore",
    "DualWriteSessionFactory",
    "SqliteSessionFactory",
    "SqliteSessionToolsPlugin",
    "_DualWriteSessionStore",
]
