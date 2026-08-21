from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aaagent.core.message import Message

if TYPE_CHECKING:
    from aaagent.providers.base import LLMProvider


@dataclass
class Session:
    id: str
    platform: str = ""
    chat_id: str = ""
    messages: list[Message] = field(default_factory=list)
    summary: str | None = None
    max_history: int = 20
    compress_threshold: float = 0.8
    last_activity: float = field(default_factory=time.time)
    system_prompt: str = ""

    @property
    def keep_after_compress(self) -> int:
        return max(1, int(self.max_history * self.compress_threshold))

    def needs_compress(self) -> bool:
        return len(self.messages) > self.max_history

    @staticmethod
    def _is_tool_message(m: Message) -> bool:
        return m.role == "tool" or (m.role == "assistant" and m.tool_calls)

    async def compress(self, provider: LLMProvider) -> None:
        if len(self.messages) <= self.max_history:
            return

        keep = self.keep_after_compress
        old = self.messages[: len(self.messages) - keep]
        if not old:
            return

        text_messages = [m for m in old if not self._is_tool_message(m)]
        if not text_messages:
            self.messages = self.messages[len(old) :]
            self.last_activity = time.time()
            return

        conversation = "\n".join(f"{m.role}: {m.content}" for m in text_messages)
        existing = f"之前的对话摘要：{self.summary}\n\n" if self.summary else ""
        prompt = (
            f"{existing}请将以下对话历史总结为一段简洁的摘要，"
            f"保留关键信息和上下文：\n\n{conversation}"
        )

        self.summary = await provider.chat([{"role": "user", "content": prompt}])
        self.messages = self.messages[len(old) :]
        self.last_activity = time.time()

    def get_context(self) -> list[dict[str, str]]:
        context: list[dict[str, str]] = []
        if self.system_prompt:
            context.append({"role": "system", "content": self.system_prompt})
        if self.summary:
            context.append({"role": "system", "content": f"对话历史摘要：{self.summary}"})
        context.extend(m.to_llm_dict() for m in self.messages)
        return context


class SessionStore:
    def __init__(
        self,
        max_history: int = 20,
        compress_threshold: float = 0.8,
        max_sessions: int = 1000,
        system_prompt: str = "",
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_history = max_history
        self._compress_threshold = compress_threshold
        self._max_sessions = max(1, max_sessions)
        self._system_prompt = system_prompt
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

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
        provider: LLMProvider | None = None,
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

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    @property
    def max_sessions(self) -> int:
        return self._max_sessions