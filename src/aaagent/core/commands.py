"""Slash command registry for chat-time meta commands.

Adapter plugins emit a `slash_command` bus event with `{text, platform,
session_id, chat_id}`. Application listens, dispatches through the
registry, and emits `slash_reply`, `slash_quit`, and
`slash_session_switch` events for adapters to act on.

This module deliberately knows nothing about the LLM, sessions, or
storage — it only routes lines starting with `/` to a registered
handler. Keeping it pure makes it trivial to unit test and reuse from
any IM adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


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


Handler = Callable[[str, SlashContext], SlashResult]


class _Cmd:
    __slots__ = ("name", "description", "handler")

    def __init__(self, name: str, description: str, handler: Handler) -> None:
        self.name = name
        self.description = description
        self.handler = handler


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._cmds: dict[str, _Cmd] = {}

    def register(self, name: str, *, description: str, handler: Handler) -> None:
        if not name.startswith("/"):
            raise ValueError(f"command name must start with '/': {name!r}")
        if " " in name:
            raise ValueError(f"command name must not contain spaces: {name!r}")
        self._cmds[name.lower()] = _Cmd(name.lower(), description, handler)

    def list_commands(self) -> list[tuple[str, str]]:
        return sorted((name, cmd.description) for name, cmd in self._cmds.items())

    def handle(
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
            result = cmd.handler(arg, ctx)
        except Exception as e:  # noqa: BLE001
            return SlashResult(
                matched=True,
                reply=f"命令 `{command}` 执行失败: {e}",
            )

        # Defensive: if a handler tries to escalate on a blacklisted path
        # somehow, drop side effects and keep only the reply.
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


def _help_handler(arg: str, ctx: SlashContext) -> SlashResult:
    reg: SlashCommandRegistry | None = getattr(ctx.app, "_commands", None)
    if reg is None:
        return SlashResult(reply="(no commands registered)")
    lines = ["Available commands:"]
    for name, desc in reg.list_commands():
        lines.append(f"  {name}  - {desc}")
    return SlashResult(reply="\n".join(lines))


def _quit_handler(arg: str, ctx: SlashContext) -> SlashResult:
    return SlashResult(stop_adapter=True, reply="Bye.")


def _session_handler(arg: str, ctx: SlashContext) -> SlashResult:
    arg = arg.strip()
    if not arg:
        return SlashResult(reply=f"Current session: {ctx.session_id}")
    new = arg if arg.startswith(f"{ctx.platform}-") else f"{ctx.platform}-{arg}"
    return SlashResult(switch_session=new, reply=f"Switched to session: {new}")


def register_builtins(reg: SlashCommandRegistry) -> None:
    reg.register(
        "/help",
        description="Show available commands",
        handler=_help_handler,
    )
    reg.register(
        "/quit",
        description="Exit chat",
        handler=_quit_handler,
    )
    reg.register(
        "/exit",
        description="Exit chat (alias of /quit)",
        handler=_quit_handler,
    )
    reg.register(
        "/session",
        description="Switch session (use: /session <name>)",
        handler=_session_handler,
    )


__all__ = [
    "Handler",
    "SlashCommandRegistry",
    "SlashContext",
    "SlashResult",
    "register_builtins",
]