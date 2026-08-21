import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aaagent.core.types import ChatResponse, LLMProvider, ToolCall


@pytest.fixture
def tmp_memory_dir(tmp_path):
    """Temporary directory to use as MemoryStore base_path."""
    return tmp_path


class FakeProvider(LLMProvider):
    """Minimal LLM provider stub for tests.

    `responses` is a queue of pre-canned ChatResponse objects; each call to
    `chat()` consumes one. `chat_calls` records (messages, tools) tuples for
    later assertions.
    """

    def __init__(
        self,
        responses: list[ChatResponse] | None = None,
        name: str = "fake",
    ) -> None:
        super().__init__(name=name, config={})
        self._responses = list(responses or [])
        self.chat_calls: list[tuple[list, object]] = []

    def push(self, response: ChatResponse) -> None:
        self._responses.append(response)

    async def chat(self, messages, tools=None, **kwargs):
        from aaagent.core.types import ChatResponse as _CR

        self.chat_calls.append((list(messages), tools))
        if not self._responses:
            return _CR(content="(no more responses)")
        return self._responses.pop(0)


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def fake_profile_provider():
    """Fake provider that returns a consolidated profile when asked."""

    class _Fake:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            from aaagent.core.types import ChatResponse

            self.calls += 1
            return ChatResponse(content="# 用户画像\n- consolidated")

    return _Fake()


__all__ = ["FakeProvider", "ToolCall", "ChatResponse"]