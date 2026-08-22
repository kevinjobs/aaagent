"""Agent loop protocol and the bundled default implementation.

The `AgentLoop` is the strategy that turns one inbound `Message` into
an assistant reply (text). It is what the user perceives as "the agent
thinking": read session context → optionally call tools → reply. The
core ships one implementation, `DefaultAgentLoop`, which does the
historical tool-iteration + provider-fallback loop. Plugins can ship
alternative loops (tree-of-thought, agent-as-tool, plan-and-execute,
…) and install them via `Application(agent_loop=...)`.

The loop is intentionally isolated from the `Application` lifecycle:
- it owns the tool-call iteration, the provider fallback chain and
  the streaming-vs-non-streaming choice;
- the `Application` owns event wiring, session persistence, background
  loops (health check, archive sweep), and adapter start/stop.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aaagent.core.message import Message
from aaagent.core.prompt import PromptBuilder

if TYPE_CHECKING:
    from aaagent.core.app import Application


logger = logging.getLogger("aaagent.agent_loop")


@dataclass
class AgentContext:
    """Inputs the loop receives per inbound message.

    The `Application` is responsible for assembling this from the
    session store, the memory store, and the tool registry. The loop
    must not reach back into the `Application` object itself.
    """

    session_id: str
    platform: str
    chat_id: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    system_prompt: str = ""
    profile: str = ""


class AgentLoop(abc.ABC):
    """Strategy interface for the per-request agent loop.

    Implementations consume an inbound `Message` + pre-built
    `AgentContext` and return the assistant's reply text. The
    `Application` persists the reply, runs memory consolidation, and
    emits `message_to_send` after the loop returns.
    """

    @abc.abstractmethod
    async def handle_message(
        self, message: Message, context: AgentContext
    ) -> str:
        ...


# ----------------------------------------------------------------------
# Default implementation
# ----------------------------------------------------------------------


class DefaultAgentLoop(AgentLoop):
    """The historical agent loop, extracted from `Application`.

    Composition over inheritance: a `DefaultAgentLoop` is constructed
    with a back-reference to the `Application` so it can call into
    `_chat_with_fallback`, `_stream_or_chat`, `_tool_registry`, and
    `_session_store` — but it never grows into the Application itself.
    """

    # Reference defaults. `Application._limits` carries the active values;
    # the loop reads them via `self._app._limits` and uses the bundled
    # values only as last-resort fallbacks (the `Limits.from_config` call
    # always sets a value).
    _DEFAULT_TOOL_WALLCLOCK_S = 120

    def __init__(self, app: "Application") -> None:
        self._app = app

    async def handle_message(
        self, message: Message, context: AgentContext
    ) -> str:
        try:
            reply_text = await self._run_tool_loop_with_limits(context)
            return _strip_think(reply_text)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "LLM call failed for session %s: %s",
                context.session_id,
                e,
                exc_info=True,
            )
            return _PUBLIC_ERROR

    # ---- internals ----------------------------------------------------

    async def _run_tool_loop_with_limits(self, context: AgentContext) -> str:
        wallclock = self._app._limits.max_tool_wallclock_s
        try:
            return await asyncio.wait_for(
                self._run_tool_loop(context),
                timeout=wallclock if wallclock > 0 else None,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Tool loop exceeded wall-clock %.1fs for session %s",
                wallclock,
                context.session_id,
            )
            return (
                f"工具循环超时（{wallclock:.0f}s），已中止。"
                "请简化请求或调整 limits.max_tool_wallclock_s。"
            )

    async def _run_tool_loop(self, context: AgentContext) -> str:
        messages = context.messages
        tools = context.tools
        if not tools:
            return await self._stream_or_chat(messages)

        max_turns = self._app._limits.max_tool_turns
        max_chars = self._app._limits.max_tool_chars
        tool_registry = self._app._tool_registry
        session_store = self._app._session_store
        bus = self._app._bus

        for turn in range(1, max_turns + 1):
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            if total_chars > max_chars:
                logger.warning(
                    "Tool loop messages exceed %d chars (%d), aborting for session %s",
                    max_chars,
                    total_chars,
                    context.session_id,
                )
                return "上下文过长，已中止。请开启新对话。"
            result = await self._app._chat_with_fallback(messages, tools=tools)

            if not result.tool_calls:
                return result.content

            tool_role_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or None,
            }
            tool_calls_dicts: list[dict[str, Any]] = []
            for tc in result.tool_calls:
                tool_calls_dicts.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                })
            tool_role_msg["tool_calls"] = tool_calls_dicts
            messages.append(tool_role_msg)

            await bus.emit(
                "tool_start",
                {
                    "session_id": context.session_id,
                    "platform": context.platform,
                    "chat_id": context.chat_id,
                    "tool_calls": result.tool_calls,
                    "turn": turn,
                },
            )

            for tc in result.tool_calls:
                t0 = time.monotonic()
                output = await tool_registry.execute(tc.name, tc.arguments)
                duration_ms = int((time.monotonic() - t0) * 1000)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": output,
                })

                await bus.emit(
                    "tool_result",
                    {
                        "session_id": context.session_id,
                        "platform": context.platform,
                        "chat_id": context.chat_id,
                        "tool_call_id": tc.id,
                        "tool_name": tc.name,
                        "arguments": tc.arguments,
                        "result": output,
                        "duration_ms": duration_ms,
                        "turn": turn,
                    },
                )

            msg = Message(
                session_id=context.session_id,
                platform=context.platform,
                chat_id=context.chat_id,
                user_id="assistant",
                content=result.content,
                role="assistant",
                tool_calls=tool_calls_dicts,
            )
            await session_store.add_message(context.session_id, msg)

            for tc in result.tool_calls:
                tool_msg = Message(
                    session_id=context.session_id,
                    platform=context.platform,
                    chat_id=context.chat_id,
                    user_id="system",
                    content=tc.arguments,
                    role="tool",
                    tool_call_id=tc.id,
                    name=tc.name,
                )
                await session_store.add_message(context.session_id, tool_msg)

        logger.warning(
            "Tool loop exceeded max turns (%d) for session %s",
            max_turns,
            context.session_id,
        )
        return "已达到最大工具调用次数。"

    async def _stream_or_chat(self, messages: list[dict[str, Any]]) -> str:
        """Prefer streaming reply if the provider supports it; else one-shot chat.

        Mirrors the historical Application._stream_or_chat exactly. Kept
        as a private helper on the loop so the alternative loop plugin
        authors can compare against the reference implementation.
        """
        providers = self._app._provider_order or (
            [self._app._provider] if self._app._provider is not None else []
        )
        if not providers:
            raise RuntimeError("No LLM provider configured")

        last_exc: Exception | None = None
        for index, provider in enumerate(providers):
            stream_fn = getattr(provider, "stream_chat", None)
            started = False
            try:
                if stream_fn is not None:
                    try:
                        chunks: list[str] = []
                        async for chunk in stream_fn(messages):
                            started = True
                            chunks.append(chunk)
                            await self._app._bus.emit("stream_token", chunk)
                        if chunks:
                            return "".join(chunks)
                    except NotImplementedError:
                        pass
                await self._app._acquire_provider_bucket(provider)
                result = await provider.chat(messages)
                return result.content
            except Exception as e:  # noqa: BLE001
                if started:
                    raise
                last_exc = e
                if index + 1 >= len(providers):
                    break
                # Reuse the framework's classifier so behaviour stays
                # consistent with `_chat_with_fallback`.
                from aaagent.core.app import _is_retryable_provider_error

                if not _is_retryable_provider_error(e, provider=provider):
                    logger.error(
                        "Provider %s failed with non-retryable error: %s",
                        provider.name,
                        e,
                    )
                    raise
                logger.warning(
                    "Provider %s failed (retryable), trying next: %s",
                    provider.name,
                    e,
                )

        assert last_exc is not None
        raise last_exc


_THINK_RE = __import__("re").compile(
    r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>",
    __import__("re").DOTALL | __import__("re").IGNORECASE,
)
_UNCLOSED_THINK_RE = __import__("re").compile(
    r"<think(?:ing)?\b[^>]*>.*\Z",
    __import__("re").DOTALL | __import__("re").IGNORECASE,
)
_PUBLIC_ERROR = "服务暂时不可用，请稍后再试。"


def _strip_think(text: str) -> str:
    if not text:
        return text
    cleaned = _THINK_RE.sub("", text)
    cleaned = _UNCLOSED_THINK_RE.sub("", cleaned)
    cleaned = __import__("re").sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


__all__ = [
    "AgentLoop",
    "AgentContext",
    "DefaultAgentLoop",
    "_strip_think",
    "_THINK_RE",
    "_UNCLOSED_THINK_RE",
    "_PUBLIC_ERROR",
]