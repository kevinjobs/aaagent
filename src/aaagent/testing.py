"""Public testing helpers shipped with the aaagent core.

Plugins (and the core test suite) can use these to build their own
pytest fixtures without duplicating boilerplate. The contents are
intentionally framework-level stand-ins; they are not part of the
production runtime and should not be imported from non-test code.

Typical use:

    from aaagent.testing import FakeProvider
    from aaagent.core.types import ChatResponse

    provider = FakeProvider(
        responses=[ChatResponse(content="hello"), ChatResponse(content="bye")]
    )

    reply = await provider.chat(messages)

    assert provider.chat_calls == [(messages, None)]
"""

from __future__ import annotations

from typing import Any

from aaagent.core.types import ChatResponse, LLMProvider


class FakeProvider(LLMProvider):
    """Minimal LLM provider stub for tests.

    `responses` is a queue of pre-canned `ChatResponse` objects; each call
    to `chat()` consumes one. `chat_calls` records `(messages, tools)`
    tuples for later assertions.

    The class is exposed under `aaagent.testing` (not `aaagent.core`) so
    the testing surface does not leak into the production namespace.
    Plugins that want their own `type: custom` provider can reference
    it as `aaagent.testing.FakeProvider` in YAML.

    Back-compat: the legacy alias `tests.conftest.FakeProvider` was
    retired when tests moved to per-package directories; use this name
    instead.
    """

    def __init__(
        self,
        responses: list[ChatResponse] | None = None,
        name: str = "fake",
    ) -> None:
        super().__init__(name=name, config={})
        self._responses = list(responses or [])
        self.chat_calls: list[tuple[list, Any]] = []

    def push(self, response: ChatResponse) -> None:
        self._responses.append(response)

    async def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.chat_calls.append((list(messages), tools))
        if not self._responses:
            return ChatResponse(content="(no more responses)")
        return self._responses.pop(0)


__all__ = ["FakeProvider"]