"""Slash-command bundle for aaagent.

Contributes four chat-time commands to the registry the core owns:

* `/session` — start a new session or switch to one by name
* `/sessions` — list known sessions
* `/compact` — force-compress the current session
* `/model` and `/models` — switch / add / list LLM providers

The core only ships `/help` and `/quit` (protocol-level commands every
adapter needs). Everything else moves here so the core can stay free of
features that can reasonably live in a plugin.

The `register(app)` function is the entry point that
`Application.__init__` calls after loading the plugin manager. It wires
all four handlers onto `app.commands`.

Compatibility: the legacy `DotenvStore` / `ConfigStore` round-trip lives
in `aaagent.core.dotenv_io` / `aaagent.core.config_io`. This plugin
imports them directly; if they were to move out of core, this plugin
would simply add a dependency on the owning package.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Awaitable

from aaagent.core.commands import SlashContext, SlashResult

logger = logging.getLogger("aaagent.plugin.shell")

_SOURCE = "shell"


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


def _current_model_line(app: Any) -> str:
    if app is None:
        return "(no app)"
    p = getattr(app, "_provider", None)
    if p is None:
        return "(no active provider)"
    model = getattr(p, "_model", None) or p.config.get("model", "?")
    return f"Current: {p.name}  model={model}  type={p.config.get('type', '?')}"


def _derive_env_name(provider: str) -> str:
    """Turn a provider name into a `${...}_API_KEY` env-var identifier.

    Non `[A-Za-z0-9_]` chars are normalised to `_`. Leading digits are
    preserved (e.g. `9ROUTER_API_KEY`).
    """
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", provider).upper()
    if not normalized:
        raise ValueError(f"cannot derive env-var name from {provider!r}")
    return f"{normalized}_API_KEY"


def _providers_sharing_env(cfg: dict, env_name: str) -> list[str]:
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


# ---- handlers ---------------------------------------------------------------

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
                "api_key": "${_PENDING}",
                "base_url": base_url,
                "model": mname,
            }
            msg = f"Added provider '{pname}' (type={ptype}, model={mname})"

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

        new_inst = app._instantiate_provider(pname, dict(providers_block[pname]))
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


# ---- plugin entry point -----------------------------------------------------

def register(app: Any) -> None:
    """Called by `Application.__init__` to wire the commands this plugin
    owns onto `app.commands`. The core owns `/help` and `/quit`; everything
    else (session / model / compact) lives here.
    """
    commands = app.commands
    commands.register(
        "/session",
        description="Start a new session (/session <name> to switch)",
        handler=_session_handler,
        source=_SOURCE,
    )
    commands.register(
        "/sessions",
        description="List all known sessions (current marked with *)",
        handler=_sessions_handler,
        source=_SOURCE,
    )
    commands.register(
        "/compact",
        description="Force-compress the current session",
        handler=_compact_handler,
        source=_SOURCE,
    )
    commands.register(
        "/model",
        description=(
            "Switch/add provider+model: /model --provider X --model Y "
            "[-new --key K --base-url U] [-default]"
        ),
        handler=_model_handler,
        source=_SOURCE,
    )
    commands.register(
        "/models",
        description="List all configured providers and their models",
        handler=_models_handler,
        source=_SOURCE,
    )


__all__ = ["register"]