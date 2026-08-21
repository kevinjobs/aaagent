from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from aaagent.core.message import Message
from aaagent.core.plugin import SessionStoreFactory
from aaagent.core.session import Session, SessionStore

if TYPE_CHECKING:
    from aaagent.core.types import LLMProvider


class InMemorySessionStore(SessionStore):
    """Process-local in-memory session store.

    Provides LRU-evicted sessions with per-session async locks and
    automatic summary compression when the message count exceeds
    `max_history * compress_threshold`.
    """

    def __init__(
        self,
        max_history: int = 20,
        compress_threshold: float = 0.8,
        max_sessions: int = 1000,
        system_prompt: str = "",
    ) -> None:
        super().__init__(
            max_history=max_history,
            compress_threshold=compress_threshold,
            max_sessions=max_sessions,
            system_prompt=system_prompt,
        )
        self._sessions: dict[str, Session] = {}

    def _evict_lru(self) -> None:
        if len(self._sessions) <= self._max_sessions:
            return
        overflow = len(self._sessions) - self._max_sessions
        oldest = sorted(
            self._sessions.items(), key=lambda kv: kv[1].last_activity
        )[:overflow]
        for sid, _ in oldest:
            self._sessions.pop(sid, None)
            self._locks.pop(sid, None)

    def get_or_create(
        self, session_id: str, platform: str = "", chat_id: str = ""
    ) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(
                id=session_id,
                platform=platform,
                chat_id=chat_id,
                max_history=self._max_history,
                compress_threshold=self._compress_threshold,
                system_prompt=self._system_prompt,
            )
            self._sessions[session_id] = session
            self._evict_lru()
        return session

    async def add_message(
        self,
        session_id: str,
        msg: Message,
        provider: "LLMProvider | None" = None,
    ) -> Session:
        async with self._get_lock(session_id):
            session = self.get_or_create(session_id, msg.platform, msg.chat_id)
            session.messages.append(msg)
            session.last_activity = time.time()
            if provider is not None and session.needs_compress():
                await session.compress(provider)
            return session

    async def get_context(self, session_id: str) -> list[dict[str, str]]:
        async with self._get_lock(session_id):
            session = self.get_or_create(session_id)
            return session.get_context()

    async def get_session(self, session_id: str) -> Session:
        async with self._get_lock(session_id):
            return self.get_or_create(session_id)

    async def drop_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._locks.pop(session_id, None)

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    @property
    def max_sessions(self) -> int:
        return self._max_sessions


class InMemorySessionFactory(SessionStoreFactory):
    """Plugin factory for the in-memory session store."""

    name = "inmemory"

    def create(self, config: dict) -> SessionStore:
        return InMemorySessionStore(
            max_history=int(config.get("max_history", 20)),
            compress_threshold=float(config.get("compress_threshold", 0.8)),
            max_sessions=int(config.get("max_sessions", 1000)),
            system_prompt=str(config.get("system_prompt", "")),
        )


__all__ = ["InMemorySessionStore", "InMemorySessionFactory"]