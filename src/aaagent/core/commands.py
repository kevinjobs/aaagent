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


def _generate_new_session_id(ctx: SlashContext, app: Any) -> str:
    from datetime import datetime

    suffix = datetime.now().strftime("%H%M%S")
    candidate = f"{ctx.platform}-new-{suffix}"
    store = getattr(app, "_session_store", None) if app else None
    if store is not None and hasattr(store, "list_sessions"):
        existing = {s.id for s in store.list_sessions()}
        n = 1
        while candidate in existing:
            candidate = f"{ctx.platform}-new-{suffix}-{n}"
            n += 1
    return candidate


def _session_handler(arg: str, ctx: SlashContext) -> SlashResult:
    arg = arg.strip()
    app = ctx.app

    if not arg:
        new_id = _generate_new_session_id(ctx, app)
        return SlashResult(
            switch_session=new_id,
            reply=f"Started new session: {new_id}",
        )

    target = arg if arg.startswith(f"{ctx.platform}-") else f"{ctx.platform}-{arg}"
    if target == ctx.session_id:
        return SlashResult(reply=f"Already in session: {target}")
    return SlashResult(switch_session=target, reply=f"Switched to session: {target}")


def _sessions_handler(arg: str, ctx: SlashContext) -> SlashResult:
    from types import SimpleNamespace

    app = ctx.app
    store = getattr(app, "_session_store", None) if app else None
    if store is None or not hasattr(store, "list_sessions"):
        return SlashResult(reply="(session store unavailable)")
    sessions = list(store.list_sessions())

    # Always surface the current session even if the store hasn't seen
    # any messages for it yet (e.g. user typed /sessions immediately
    # after starting the CLI before sending any user message). Without
    # this, the user is told "No sessions yet" while clearly in one.
    seen_ids = {s.id for s in sessions}
    if ctx.session_id not in seen_ids:
        sessions.append(
            SimpleNamespace(
                id=ctx.session_id,
                last_activity=0,
                messages=[],
                summary="",
            )
        )

    if not sessions:
        return SlashResult(reply="No sessions yet.")

    current = ctx.session_id
    lines = []
    for s in sorted(
        sessions, key=lambda x: getattr(x, "last_activity", 0), reverse=True
    ):
        marker = "*" if s.id == current else " "
        msg_count = len(getattr(s, "messages", []) or [])
        summary = getattr(s, "summary", "") or ""
        preview = (
            f"  [summary: {summary[:30]}...]" if summary else ""
        )
        lines.append(f"{marker} {s.id}  ({msg_count} msgs){preview}")
    return SlashResult(reply="Sessions:\n" + "\n".join(lines))


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
        "/session",
        description="Start a new session (/session <name> to switch)",
        handler=_session_handler,
    )
    reg.register(
        "/sessions",
        description="List all known sessions (current marked with *)",
        handler=_sessions_handler,
    )


__all__ = [
    "Handler",
    "SlashCommandRegistry",
    "SlashContext",
    "SlashResult",
    "register_builtins",
]