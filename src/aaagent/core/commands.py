"""Slash command registry for chat-time meta commands.

Adapter plugins emit a `slash_command` bus event with `{text, platform,
session_id, chat_id}`. Application listens, dispatches through the
registry, and emits `slash_reply`, `slash_quit`, and
`slash_session_switch` events for adapters to act on.

This module deliberately knows nothing about the LLM, sessions, or
storage — it only routes lines starting with `/` to a registered
handler. Keeping it pure makes it trivial to unit test and reuse from
any IM adapter.

The core ships only the two protocol-level commands that every adapter
needs out of the box:

  * `/help` — list registered commands
  * `/quit` — stop the current adapter

Everything else — `/model`, `/compact`, `/session`, `/sessions`, etc. —
lives in plugins (see `aaagent-plugin-shell` for the canonical chat-time
bundle). Plugins register their handlers with the same `SlashCommandRegistry`
the core owns.

Handlers are `async` so they can do IO (provider calls for /compact,
disk writes for /model persistence, etc.) without blocking the bus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("aaagent.commands")


@dataclass
class SlashContext:
    platform: str
    session_id: str
    chat_id: str
    app: Any = None


@dataclass
class SlashResult:
    matched: bool = False
    suppressed: bool = False
    reply: str | None = None
    stop_adapter: bool = False
    switch_session: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[str, SlashContext], Awaitable[SlashResult]]


class _Cmd:
    __slots__ = ("name", "description", "source", "handler")

    def __init__(
        self,
        name: str,
        description: str,
        handler: Handler,
        source: str = "plugin",
    ) -> None:
        self.name = name
        self.description = description
        self.source = source
        self.handler = handler


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._cmds: dict[str, _Cmd] = {}

    def register(
        self,
        name: str,
        *,
        description: str,
        handler: Handler,
        source: str = "plugin",
    ) -> None:
        """Register a slash command.

        `source` is an opaque label used by `/help` to mark which plugin
        (or "core") contributed the command. The core passes "core";
        plugins should pass their own package or distribution name.
        """
        if not name.startswith("/"):
            raise ValueError(f"command name must start with '/': {name!r}")
        if " " in name:
            raise ValueError(f"command name must not contain spaces: {name!r}")
        self._cmds[name.lower()] = _Cmd(
            name.lower(), description, handler, source=source
        )

    def list_commands(self) -> list[tuple[str, str, str]]:
        """Return `(name, desc, source)` tuples sorted by name."""
        return sorted(
            (name, cmd.description, cmd.source) for name, cmd in self._cmds.items()
        )

    async def handle(
        self,
        text: str,
        ctx: SlashContext,
        *,
        blacklist: set[str] | None = None,
    ) -> SlashResult:
        if not isinstance(text, str) or not text.startswith("/"):
            return SlashResult()

        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        cmd = self._cmds.get(command)
        if cmd is None:
            return SlashResult()

        if blacklist is not None and command in blacklist:
            return SlashResult(
                matched=True,
                suppressed=True,
                reply=(
                    f"此平台（{ctx.platform}）不支持 `{command}` 命令。"
                    "如需退出请直接关闭客户端。"
                ),
            )

        try:
            result = await cmd.handler(arg, ctx)
        except Exception as e:  # noqa: BLE001
            return SlashResult(
                matched=True,
                reply=f"命令 `{command}` 执行失败: {e}",
            )

        if result.suppressed:
            return SlashResult(
                matched=True,
                suppressed=True,
                reply=result.reply
                or (
                    f"此平台（{ctx.platform}）不支持 `{command}` 命令。"
                ),
            )

        if not result.matched:
            result.matched = True
        return result


def _help_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        reg: SlashCommandRegistry | None = getattr(ctx.app, "_commands", None)
        if reg is None:
            return SlashResult(reply="(no commands registered)")
        lines = ["Available commands:"]
        for name, desc, source in reg.list_commands():
            tag = f" [{source}]" if source != "core" else ""
            lines.append(f"  {name}  - {desc}{tag}")
        return SlashResult(reply="\n".join(lines))

    return _h()


def _quit_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        return SlashResult(stop_adapter=True, reply="Bye.")

    return _h()


def register_builtins(reg: SlashCommandRegistry) -> None:
    """Register the protocol-level commands every chat needs.

    Plugins (e.g. `aaagent-plugin-shell`) add their own handlers via the
    same registry; this function only owns `/help` and `/quit`.
    """
    reg.register(
        "/help",
        description="Show available commands",
        handler=_help_handler,
        source="core",
    )
    reg.register(
        "/quit",
        description="Exit chat",
        handler=_quit_handler,
        source="core",
    )


__all__ = [
    "Handler",
    "SlashCommandRegistry",
    "SlashContext",
    "SlashResult",
    "register_builtins",
]