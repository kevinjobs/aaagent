from __future__ import annotations

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

    @property
    def _compress_limit(self) -> int:
        return int(self.max_history * self.compress_threshold)

    def needs_compress(self) -> bool:
        return len(self.messages) >= self._compress_limit

    async def compress(self, provider: LLMProvider) -> None:
        if len(self.messages) < 2:
            return

        old_messages = self.messages[: -self._compress_limit] if self._compress_limit > 0 else self.messages
        if not old_messages:
            return

        conversation = "\n".join(f"{m.role}: {m.content}" for m in old_messages)
        existing = f"之前的对话摘要：{self.summary}\n\n" if self.summary else ""

        prompt = (
            f"{existing}请将以下对话历史总结为一段简洁的摘要，"
            f"保留关键信息和上下文：\n\n{conversation}"
        )

        self.summary = await provider.chat([{"role": "user", "content": prompt}])
        self.messages = self.messages[len(old_messages):]

    def get_context(self) -> list[dict[str, str]]:
        context: list[dict[str, str]] = []
        if self.summary:
            context.append({"role": "system", "content": f"对话历史摘要：{self.summary}"})
        context.extend(m.to_llm_dict() for m in self.messages)
        return context


class SessionStore:
    def __init__(self, max_history: int = 20, compress_threshold: float = 0.8) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_history = max_history
        self._compress_threshold = compress_threshold

    def get_or_create(
        self, session_id: str, platform: str = "", chat_id: str = ""
    ) -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(
                id=session_id,
                platform=platform,
                chat_id=chat_id,
                max_history=self._max_history,
                compress_threshold=self._compress_threshold,
            )
        return self._sessions[session_id]

    async def add_message(self, session_id: str, msg: Message) -> Session:
        session = self.get_or_create(session_id, msg.platform, msg.chat_id)
        session.messages.append(msg)
        return session

    async def get_context(self, session_id: str) -> list[dict[str, str]]:
        session = self.get_or_create(session_id)
        return session.get_context()

    async def maybe_compress(self, session_id: str, provider: LLMProvider) -> None:
        session = self.get_or_create(session_id)
        if session.needs_compress():
            await session.compress(provider)

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())
