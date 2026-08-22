"""Slash command registry for chat-time meta commands.

Adapter plugins emit a `slash_command` bus event with `{text, platform,
session_id, chat_id}`. Application listens, dispatches through the
registry, and emits `slash_reply`, `slash_quit`, and
`slash_session_switch` events for adapters to act on.

This module deliberately knows nothing about the LLM, sessions, or
storage — it only routes lines starting with `/` to a registered
handler. Keeping it pure makes it trivial to unit test and reuse from
any IM adapter.

Handlers are `async` so they can do IO (provider calls for /compact,
disk writes for /model persistence, etc.) without blocking the bus.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
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


def _parse_flags(arg: str) -> dict[str, Any]:
    """Parse ' --foo bar -baz --qux' style flags.

    Supports `--long value`, `--flag` (boolean), `-x value`, `-x` (boolean).
    Values that look like flags start with `-` are not consumed as values.
    """
    out: dict[str, Any] = {}
    if not arg:
        return out
    try:
        parts = shlex.split(arg)
    except ValueError:
        parts = arg.split()
    i = 0
    while i < len(parts):
        p = parts[i]
        if p.startswith("--") and len(p) > 2:
            key = p[2:].replace("-", "_")
            if i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                out[key] = parts[i + 1]
                i += 2
            else:
                out[key] = True
                i += 1
        elif p.startswith("-") and len(p) > 1:
            name = p[1:]
            if i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                out[name] = parts[i + 1]
                i += 2
            else:
                out[name] = True
                i += 1
        else:
            i += 1
    return out


def _help_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        reg: SlashCommandRegistry | None = getattr(ctx.app, "_commands", None)
        if reg is None:
            return SlashResult(reply="(no commands registered)")
        lines = ["Available commands:"]
        for name, desc in reg.list_commands():
            lines.append(f"  {name}  - {desc}")
        return SlashResult(reply="\n".join(lines))

    return _h()


def _quit_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        return SlashResult(stop_adapter=True, reply="Bye.")

    return _h()


def _generate_new_session_id(ctx: SlashContext, app: Any) -> str:
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


def _session_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        arg_ = arg.strip()
        app = ctx.app

        if not arg_:
            new_id = _generate_new_session_id(ctx, app)
            return SlashResult(
                switch_session=new_id,
                reply=f"Started new session: {new_id}",
            )

        target = arg_ if arg_.startswith(f"{ctx.platform}-") else f"{ctx.platform}-{arg_}"
        if target == ctx.session_id:
            return SlashResult(reply=f"Already in session: {target}")
        return SlashResult(switch_session=target, reply=f"Switched to session: {target}")

    return _h()


def _sessions_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        from types import SimpleNamespace

        app = ctx.app
        store = getattr(app, "_session_store", None) if app else None
        if store is None or not hasattr(store, "list_sessions"):
            return SlashResult(reply="(session store unavailable)")
        sessions = list(store.list_sessions())

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

    return _h()


def _current_model_line(app: Any) -> str:
    if app is None:
        return "(no app)"
    p = getattr(app, "_provider", None)
    if p is None:
        return "(no active provider)"
    model = getattr(p, "_model", None) or p.config.get("model", "?")
    return f"Current: {p.name}  model={model}  type={p.config.get('type', '?')}"


def _compact_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        app = ctx.app
        if app is None:
            return SlashResult(reply="(no app context)")
        session_store = getattr(app, "_session_store", None)
        provider = getattr(app, "_provider", None)
        if session_store is None:
            return SlashResult(reply="(no session store)")
        if provider is None:
            return SlashResult(reply="(no active provider)")

        try:
            session = await session_store.get_session(ctx.session_id)
            msg_count = len(session.messages)
            before_chars = sum(
                len(m.content or "") for m in session.messages
            )
            if msg_count < 2:
                return SlashResult(
                    reply=(
                        f"Nothing to compact in {ctx.session_id}: "
                        f"{msg_count} message(s)."
                    )
                )
            summarized = await session.force_compress(provider)
            after_chars = sum(
                len(m.content or "") for m in session.messages
            )
            if not summarized:
                return SlashResult(
                    reply=(
                        f"No text content to summarize in {ctx.session_id} "
                        "(only tool messages)."
                    )
                )
            return SlashResult(
                reply=(
                    f"Compacted {ctx.session_id}: "
                    f"{msg_count} → {len(session.messages)} messages, "
                    f"~{before_chars} → ~{after_chars} chars."
                )
            )
        except Exception as e:  # noqa: BLE001
            return SlashResult(reply=f"Compact failed: {e}")

    return _h()


def _models_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        app = ctx.app
        if app is None:
            return SlashResult(reply="(no app context)")
        if not app._providers:
            return SlashResult(reply="No providers configured.")
        cfg = app._config
        meta = (cfg.get("providers", {}) or {}).get("_meta", {}) if cfg else {}
        default_name = meta.get("default", "")
        current_name = app._provider.name if app._provider else ""
        lines = ["Available providers:"]
        for name in sorted(app._providers.keys()):
            p = app._providers[name]
            model = getattr(p, "_model", None) or p.config.get("model", "?")
            marker = "*" if name == current_name else " "
            suffix = " (default)" if name == default_name else ""
            lines.append(
                f"{marker} {name}{suffix}  model={model}  "
                f"type={p.config.get('type', '?')}"
            )
        return SlashResult(reply="\n".join(lines))

    return _h()


def _derive_env_name(provider: str) -> str:
    """Turn a provider name into a `${...}_API_KEY` env-var identifier.

    Provider names that contain non `[A-Za-z0-9_]` characters are
    normalised by replacing such characters with `_`. Leading digits are
    preserved (shell allows env-var names like `9ROUTER_API_KEY` even
    though Python identifiers don't).
    """
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", provider).upper()
    if not normalized:
        raise ValueError(f"cannot derive env-var name from {provider!r}")
    return f"{normalized}_API_KEY"


def _providers_sharing_env(cfg: dict, env_name: str) -> list[str]:
    """List every provider entry that references `${env_name}`.

    Used by `/model -new --key` to warn the operator when an overwrite
    will affect more than the targeted provider (D2).
    """
    ref = "${" + env_name + "}"
    out: list[str] = []
    providers = cfg.get("providers", {}) or {}
    for name, body in providers.items():
        if name == "_meta":
            continue
        if isinstance(body, dict) and body.get("api_key") == ref:
            out.append(name)
    return out


def _persist_api_key(app: Any, pname: str, key: str) -> tuple[str, str, list[str]]:
    """Write `key` to `.env` and update the in-process env so the new
    value is visible to the provider's next instantiation.

    Returns `(env_name, dotenv_result, shared_providers)`. The caller
    is responsible for:
      - deciding whether to surface a WARN for `"overwrite"`
      - rewriting `providers[pname]["api_key"]` to `"${env_name}"`
      - calling `app._setup_providers()` to pick up the new env var
    """
    env_name = _derive_env_name(pname)
    dotenv = getattr(app, "_dotenv", None)
    if dotenv is None:
        raise RuntimeError("DotenvStore not initialised on Application")
    result = dotenv.set(env_name, key)
    os.environ[env_name] = key
    shared = _providers_sharing_env(app._config, env_name)
    logger.info(
        "/model key update: provider=%s key_env=%s dotenv=%s shared=%s",
        pname, env_name, result, ",".join(shared) or "-",
    )
    return env_name, result, shared


def _model_handler(arg: str, ctx: SlashContext) -> Awaitable[SlashResult]:
    async def _h() -> SlashResult:
        app = ctx.app
        if app is None:
            return SlashResult(reply="(no app context)")

        args = _parse_flags(arg)
        pname = args.get("provider")
        mname = args.get("model")
        if not pname or not mname:
            return SlashResult(reply=_current_model_line(app))

        is_new = args.get("new") is True
        is_default = args.get("default") is True
        # `--key ""` (empty string) is treated as "no key change" so
        # `/model --provider X --model Y` works the same whether the
        # caller omits `--key` or writes `--key ""`.
        key_arg = args.get("key")
        key: str | None = key_arg if isinstance(key_arg, str) and key_arg else None
        base_url = args.get("base_url")
        ptype = args.get("type", "openai_compatible")

        cfg = app._config
        if cfg is None:
            return SlashResult(reply="(config unavailable)")

        persistence = (cfg.get("limits", {}) or {}).get("provider_persistence", "disk")
        persist_to_disk = persistence != "memory"

        providers_block = cfg.setdefault("providers", {})
        existing = pname in providers_block
        if existing:
            providers_block[pname]["model"] = mname
            msg = f"Switched '{pname}' to model={mname}"
        else:
            if not is_new:
                return SlashResult(
                    reply=(
                        f"Provider '{pname}' not configured. "
                        f"Add with: /model --provider {pname} "
                        f"--model {mname} -new --key <KEY> --base-url <URL>"
                    )
                )
            if not key or not base_url:
                return SlashResult(
                    reply=(
                        "Creating a new provider requires both --key and "
                        "--base-url. Example: /model --provider foo "
                        "--model bar -new --key sk-xxx "
                        "--base-url https://api.foo.com/v1"
                    )
                )
            providers_block[pname] = {
                "type": ptype,
                "enabled": True,
                "api_key": "${_PENDING}",  # placeholder, replaced below
                "base_url": base_url,
                "model": mname,
            }
            msg = (
                f"Added provider '{pname}' "
                f"(type={ptype}, model={mname})"
            )

        warn = ""
        env_name: str | None = None
        if key:
            try:
                env_name, dotenv_result, shared = _persist_api_key(
                    app, pname, key
                )
            except Exception as e:  # noqa: BLE001
                return SlashResult(
                    reply=(
                        f"Failed to persist API key to .env: {e}. "
                        "Check that .env is writable."
                    )
                )
            providers_block[pname]["api_key"] = "${" + env_name + "}"
            if dotenv_result == "overwrite" and shared:
                others = [s for s in shared if s != pname]
                if others:
                    warn = (
                        f"  ⚠ overwrote {env_name}; also used by: "
                        + ", ".join(sorted(others))
                    )

        new_inst = app._instantiate_provider(
            pname, dict(providers_block[pname])
        )
        if new_inst is None:
            return SlashResult(reply=f"Failed to instantiate '{pname}'")
        app._providers[pname] = new_inst

        if is_default:
            providers_block = cfg.setdefault("providers", {})
            meta = providers_block.setdefault("_meta", {})
            meta["default"] = pname
            app._provider = new_inst
            if new_inst in app._provider_order:
                app._provider_order.remove(new_inst)
            app._provider_order.insert(0, new_inst)
            app._init_provider_buckets()
            msg += " (set as default)"
        else:
            app._provider = new_inst
            if new_inst in app._provider_order:
                app._provider_order.remove(new_inst)
            app._provider_order.insert(0, new_inst)

        persisted = False
        if persist_to_disk:
            try:
                app._config_store.save(cfg)
                persisted = True
            except Exception as e:  # noqa: BLE001
                msg += f" [WARNING: persist failed: {e}]"

        if persisted:
            if env_name:
                msg += f" [key→{env_name} in .env; persisted to config.yaml]"
            else:
                msg += " [persisted to config.yaml]"

        if warn:
            msg += warn

        return SlashResult(reply=msg)

    return _h()


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
    reg.register(
        "/compact",
        description="Force-compress the current session",
        handler=_compact_handler,
    )
    reg.register(
        "/model",
        description=(
            "Switch/add provider+model: /model --provider X --model Y "
            "[-new --key K --base-url U] [-default]"
        ),
        handler=_model_handler,
    )
    reg.register(
        "/models",
        description="List all configured providers and their models",
        handler=_models_handler,
    )


__all__ = [
    "Handler",
    "SlashCommandRegistry",
    "SlashContext",
    "SlashResult",
    "register_builtins",
    "_parse_flags",
]