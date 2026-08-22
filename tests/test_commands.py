from __future__ import annotations

from types import SimpleNamespace

import pytest

from aaagent.core.commands import (
    SlashCommandRegistry,
    SlashContext,
    SlashResult,
    _parse_flags,
    register_builtins,
)


def _ctx(reg: SlashCommandRegistry | None = None, platform: str = "cli", session_id: str = "cli-default") -> SlashContext:
    app = SimpleNamespace(_commands=reg) if reg is not None else None
    return SlashContext(
        platform=platform, session_id=session_id, chat_id=session_id, app=app
    )


def test_register_rejects_invalid_name():
    reg = SlashCommandRegistry()

    async def h(*a, **k):
        return SlashResult()

    with pytest.raises(ValueError, match="must start with '/'"):
        reg.register("foo", description="x", handler=h)
    with pytest.raises(ValueError, match="must not contain spaces"):
        reg.register("/foo bar", description="x", handler=h)


@pytest.mark.asyncio
async def test_handle_ignores_non_slash():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("hello world", _ctx())
    assert result.matched is False
    assert result.reply is None


@pytest.mark.asyncio
async def test_handle_unknown_command():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/nope arg", _ctx())
    assert result.matched is False


@pytest.mark.asyncio
async def test_help_lists_builtins():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/help", _ctx(reg=reg))
    assert result.matched is True
    assert "/help" in (result.reply or "")
    assert "/quit" in (result.reply or "")
    assert "/session" in (result.reply or "")
    assert "/sessions" in (result.reply or "")
    assert "/compact" in (result.reply or "")
    assert "/model" in (result.reply or "")
    assert "/models" in (result.reply or "")
    assert "/exit" not in (result.reply or "")


@pytest.mark.asyncio
async def test_quit_signals_stop_adapter():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/quit", _ctx())
    assert result.matched is True
    assert result.stop_adapter is True
    assert result.reply == "Bye."


@pytest.mark.asyncio
async def test_exit_no_longer_registered():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/exit", _ctx())
    assert result.matched is False


@pytest.mark.asyncio
async def test_session_no_arg_starts_new_session():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/session", _ctx())
    assert result.matched is True
    assert result.switch_session is not None
    assert result.switch_session.startswith("cli-new-")
    assert "Started new session" in (result.reply or "")


@pytest.mark.asyncio
async def test_session_with_arg_switches():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/session bar", _ctx())
    assert result.matched is True
    assert result.switch_session == "cli-bar"
    assert "Switched to session" in (result.reply or "")


@pytest.mark.asyncio
async def test_session_strips_already_prefixed_arg():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/session cli-bar", _ctx())
    assert result.switch_session == "cli-bar"


@pytest.mark.asyncio
async def test_session_same_id_noops():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/session cli-default", _ctx(session_id="cli-default"))
    assert result.matched is True
    assert result.switch_session is None
    assert "Already in session" in (result.reply or "")


@pytest.mark.asyncio
async def test_sessions_lists_with_current_marker():
    fake_session_a = SimpleNamespace(
        id="cli-default", last_activity=100, messages=[1, 2, 3], summary=""
    )
    fake_session_b = SimpleNamespace(
        id="cli-foo", last_activity=200, messages=[], summary="old chat"
    )
    fake_store = SimpleNamespace(list_sessions=lambda: [fake_session_a, fake_session_b])
    app = SimpleNamespace(_commands=None, _session_store=fake_store)
    ctx = SlashContext(platform="cli", session_id="cli-default", chat_id="x", app=app)

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "Sessions:" in (result.reply or "")
    assert "* cli-default" in (result.reply or "")
    assert " cli-foo" in (result.reply or "")
    assert "[summary: old chat" in (result.reply or "")


@pytest.mark.asyncio
async def test_sessions_no_store_graceful():
    app = SimpleNamespace(_commands=None, _session_store=None)
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "unavailable" in (result.reply or "")


@pytest.mark.asyncio
async def test_sessions_includes_current_when_store_empty():
    fake_store = SimpleNamespace(list_sessions=lambda: [])
    app = SimpleNamespace(_commands=None, _session_store=fake_store)
    ctx = SlashContext(
        platform="cli",
        session_id="cli-default",
        chat_id="cli-default",
        app=app,
    )

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/sessions", ctx)
    assert result.matched is True
    assert "No sessions yet" not in (result.reply or "")
    assert "* cli-default" in (result.reply or "")
    assert "(0 msgs)" in (result.reply or "")


@pytest.mark.asyncio
async def test_sessions_includes_current_when_store_has_others_but_not_current():
    fake_session_a = SimpleNamespace(
        id="cli-foo", last_activity=100, messages=[1], summary=""
    )
    fake_store = SimpleNamespace(list_sessions=lambda: [fake_session_a])
    app = SimpleNamespace(_commands=None, _session_store=fake_store)
    ctx = SlashContext(
        platform="cli",
        session_id="cli-default",
        chat_id="cli-default",
        app=app,
    )

    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/sessions", ctx)
    assert "* cli-default" in (result.reply or "")
    assert " cli-foo" in (result.reply or "")


@pytest.mark.asyncio
async def test_blacklisted_command_returns_unsupported_reply():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")
    result = await reg.handle("/quit", ctx, blacklist={"/quit"})
    assert result.matched is True
    assert result.suppressed is True
    assert "feishu" in (result.reply or "")
    assert result.stop_adapter is False
    assert result.switch_session is None


@pytest.mark.asyncio
async def test_blacklisted_command_drops_side_effects_even_if_handler_returns_them():
    reg = SlashCommandRegistry()

    async def evil(arg, ctx):
        return SlashResult(matched=True, stop_adapter=True, switch_session="evil")

    reg.register("/evil", description="evil", handler=evil)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")
    result = await reg.handle("/evil", ctx, blacklist={"/evil"})
    assert result.suppressed is True
    assert result.stop_adapter is False
    assert result.switch_session is None


@pytest.mark.asyncio
async def test_handler_exception_is_caught():
    reg = SlashCommandRegistry()

    async def boom(arg, ctx):
        raise RuntimeError("nope")

    reg.register("/boom", description="boom", handler=boom)
    result = await reg.handle("/boom", _ctx())
    assert result.matched is True
    assert "nope" in (result.reply or "")
    assert result.stop_adapter is False


@pytest.mark.asyncio
async def test_custom_command_registration():
    reg = SlashCommandRegistry()
    register_builtins(reg)

    async def echo(arg, ctx):
        return SlashResult(reply=f"echo: {arg}")

    reg.register("/echo", description="Echo input", handler=echo)
    result = await reg.handle("/echo hello", _ctx())
    assert result.matched is True
    assert result.reply == "echo: hello"


@pytest.mark.asyncio
async def test_list_commands_sorted_by_name():
    reg = SlashCommandRegistry()
    register_builtins(reg)

    async def h(*a, **k):
        return SlashResult()

    reg.register("/aaa", description="first", handler=h)
    names = [n for n, _ in reg.list_commands()]
    assert names == sorted(names)
    assert "/aaa" in names
    assert "/help" in names


@pytest.mark.asyncio
async def test_case_insensitive_command_lookup():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/QUIT", _ctx())
    assert result.matched is True
    assert result.stop_adapter is True


# ----------------------------------------------------------------------
# /compact
# ----------------------------------------------------------------------


class _FakeProvider:
    """Mirror tests/test_session.py — Session.compress expects chat() to
    return a string for the summary assignment."""

    def __init__(self, summary: str = "<summary>") -> None:
        self._summary = summary

    async def chat(self, messages, tools=None, **kwargs):
        return self._summary


class _FakeMessage:
    def __init__(self, role: str, content: str = "") -> None:
        self.role = role
        self.content = content
        self.tool_calls = None


@pytest.mark.asyncio
async def test_compact_no_session_store():
    reg = SlashCommandRegistry()
    register_builtins(reg)
    app = SimpleNamespace(_session_store=None, _provider=_FakeProvider())
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    result = await reg.handle("/compact", ctx)
    assert "no session store" in (result.reply or "")


@pytest.mark.asyncio
async def test_compact_no_provider():
    fake_store = SimpleNamespace()
    app = SimpleNamespace(_session_store=fake_store, _provider=None)
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/compact", ctx)
    assert "no active provider" in (result.reply or "")


@pytest.mark.asyncio
async def test_compact_too_few_messages():
    class _Sess:
        def __init__(self):
            self.messages = [_FakeMessage("user", "hi")]

    class _Store:
        async def get_session(self, sid):
            return _Sess()

    app = SimpleNamespace(
        _session_store=_Store(), _provider=_FakeProvider()
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/compact", ctx)
    assert "Nothing to compact" in (result.reply or "")
    assert "1 message" in (result.reply or "")


@pytest.mark.asyncio
async def test_compact_succeeds_and_reports_savings():
    from aaagent.core.session import Session

    sess = Session(
        id="cli-test",
        messages=[
            _FakeMessage("user", "first user message"),
            _FakeMessage("assistant", "first assistant reply"),
            _FakeMessage("user", "second user message"),
            _FakeMessage("assistant", "second assistant reply"),
        ],
    )

    class _Store:
        async def get_session(self, sid):
            return sess

    app = SimpleNamespace(
        _session_store=_Store(), _provider=_FakeProvider("summary!")
    )
    ctx = SlashContext(platform="cli", session_id="cli-test", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/compact", ctx)
    assert "Compacted" in (result.reply or "")
    assert "4" in (result.reply or "")
    assert sess.summary == "summary!"


# ----------------------------------------------------------------------
# /models
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_no_providers():
    app = SimpleNamespace(_providers={}, _provider=None, _config={})
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/models", ctx)
    assert "No providers" in (result.reply or "")


@pytest.mark.asyncio
async def test_models_lists_with_default_and_current():
    class _P:
        def __init__(self, name, model, ptype):
            self.name = name
            self._model = model
            self.config = {"model": model, "type": ptype}

    p_a = _P("minmax", "M3", "openai_compatible")
    p_b = _P("deepseek", "chat", "openai_compatible")
    app = SimpleNamespace(
        _providers={"minmax": p_a, "deepseek": p_b},
        _provider=p_a,
        _config={"providers": {"_meta": {"default": "minmax"}}},
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/models", ctx)
    assert "* minmax (default)" in (result.reply or "")
    assert " deepseek" in (result.reply or "")
    assert "model=M3" in (result.reply or "")
    assert "model=chat" in (result.reply or "")


# ----------------------------------------------------------------------
# /model
# ----------------------------------------------------------------------


class _FakeConfigStore:
    def __init__(self) -> None:
        self.saved: list = []

    def save(self, cfg) -> None:
        self.saved.append(cfg)


class _FakeDotenv:
    """Captures every set()/unset() call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._store: dict[str, str] = {}

    def set(self, key: str, value: str) -> str:
        existing = self._store.get(key)
        if existing == value:
            self.calls.append((key, value, "noop"))
            return "noop"
        result = "set" if existing is None else "overwrite"
        self._store[key] = value
        self.calls.append((key, value, result))
        return result

    def read(self, key: str) -> str | None:
        return self._store.get(key)


class _FakeApp:
    def __init__(
        self,
        providers_cfg: dict | None = None,
        default_provider: str = "",
        config_store: Any = None,
        dotenv: Any = None,
    ) -> None:
        from ruamel.yaml.comments import CommentedMap

        providers = CommentedMap()
        for name, p in (providers_cfg or {}).items():
            inner = CommentedMap()
            for k, v in p.items():
                inner[k] = v
            providers[name] = inner
        self._config = CommentedMap()
        self._config["providers"] = providers
        # Routing metadata lives under providers._meta (not at the
        # top level — matches the new config layout).
        meta = CommentedMap()
        meta["default"] = default_provider
        providers["_meta"] = meta
        self._providers: dict = {}
        self._provider_order: list = []
        self._provider: Any = None
        self._config_store = config_store or _FakeConfigStore()
        self._dotenv = dotenv if dotenv is not None else _FakeDotenv()
        self._provider_buckets: dict = {}
        self._instantiated: list = []

        for name, p in (providers_cfg or {}).items():
            inst = _P(name, p.get("model", ""), p.get("type", "openai_compatible"))
            self._instantiated.append((name, dict(p)))
            self._providers[name] = inst
            self._provider_order.append(inst)

        if default_provider in self._providers:
            self._provider = self._providers[default_provider]
        elif self._providers:
            self._provider = self._provider_order[0]

    def _instantiate_provider(self, name: str, cfg: dict):
        self._instantiated.append((name, dict(cfg)))
        inst = _P(name, cfg.get("model", ""), cfg.get("type", "openai_compatible"))
        return inst

    def _init_provider_buckets(self):
        self._provider_buckets = {}


class _P:
    def __init__(self, name: str, model: str = "", ptype: str = "openai_compatible") -> None:
        self.name = name
        self._model = model
        self.config = {"model": model, "type": ptype}


@pytest.mark.asyncio
async def test_model_no_args_reports_current():
    app = _FakeApp(
        providers_cfg={"minmax": {"model": "M3", "type": "openai_compatible"}},
        default_provider="minmax",
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/model", ctx)
    assert "Current:" in (result.reply or "")
    assert "minmax" in (result.reply or "")
    assert "M3" in (result.reply or "")


@pytest.mark.asyncio
async def test_model_switch_existing():
    app = _FakeApp(
        providers_cfg={"minmax": {"model": "M3", "type": "openai_compatible"}},
        default_provider="minmax",
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider minmax --model M-2", ctx
    )
    assert "Switched" in (result.reply or "")
    assert "M-2" in (result.reply or "")
    assert "persisted" in (result.reply or "")
    assert app._config["providers"]["minmax"]["model"] == "M-2"


@pytest.mark.asyncio
async def test_model_unknown_provider_without_new_errors():
    app = _FakeApp(providers_cfg={"minmax": {"model": "M3"}})
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle("/model --provider ghost --model gpt-x", ctx)
    assert "not configured" in (result.reply or "")
    assert "ghost" in (result.reply or "")  # example usage mentions it
    assert "Add with" in (result.reply or "")


@pytest.mark.asyncio
async def test_model_new_requires_key_and_base_url():
    app = _FakeApp(providers_cfg={"minmax": {"model": "M3"}})
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider foo --model bar -new", ctx
    )
    assert "requires both --key and --base-url" in (result.reply or "")


@pytest.mark.asyncio
async def test_model_new_creates_provider_and_persists():
    cfg_store = _FakeConfigStore()
    dotenv = _FakeDotenv()
    app = _FakeApp(
        providers_cfg={"minmax": {"model": "M3"}},
        config_store=cfg_store,
        dotenv=dotenv,
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider foo --model bar -new "
        "--key sk-xyz --base-url https://api.foo.com/v1",
        ctx,
    )
    assert "Added provider 'foo'" in (result.reply or "")
    assert "foo" in app._providers
    assert app._providers["foo"]._model == "bar"
    # Key goes to .env, NOT config.yaml
    assert app._config["providers"]["foo"]["api_key"] == "${FOO_API_KEY}"
    assert app._config["providers"]["foo"]["base_url"] == "https://api.foo.com/v1"
    # Dotenv recorded the set
    assert dotenv.calls == [("FOO_API_KEY", "sk-xyz", "set")]
    assert dotenv.read("FOO_API_KEY") == "sk-xyz"
    # os.environ updated for the next provider instantiation
    import os as _os
    assert _os.environ.get("FOO_API_KEY") == "sk-xyz"
    assert len(cfg_store.saved) == 1


@pytest.mark.asyncio
async def test_model_new_sets_default_when_flagged():
    cfg_store = _FakeConfigStore()
    app = _FakeApp(
        providers_cfg={"minmax": {"model": "M3"}},
        default_provider="minmax",
        config_store=cfg_store,
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider foo --model bar -new -default "
        "--key sk-xyz --base-url https://api.foo.com/v1",
        ctx,
    )
    assert "set as default" in (result.reply or "")
    assert app._config["providers"]["_meta"]["default"] == "foo"
    assert app._provider.name == "foo"
    assert app._config["providers"]["foo"]["api_key"] == "${FOO_API_KEY}"


@pytest.mark.asyncio
async def test_model_default_flag_updates_default_provider():
    cfg_store = _FakeConfigStore()
    app = _FakeApp(
        providers_cfg={
            "minmax": {"model": "M3"},
            "deepseek": {"model": "chat"},
        },
        default_provider="minmax",
        config_store=cfg_store,
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider deepseek --model coder -default", ctx
    )
    assert "set as default" in (result.reply or "")
    assert app._config["providers"]["_meta"]["default"] == "deepseek"


@pytest.mark.asyncio
async def test_model_persist_failure_reported_but_not_raised():
    class _FailingStore:
        def save(self, cfg):
            raise RuntimeError("disk full")

    app = _FakeApp(
        providers_cfg={"minmax": {"model": "M3"}},
        config_store=_FailingStore(),
    )
    ctx = SlashContext(platform="cli", session_id="cli-x", chat_id="x", app=app)
    reg = SlashCommandRegistry()
    register_builtins(reg)
    result = await reg.handle(
        "/model --provider minmax --model M-2", ctx
    )
    assert "WARNING" in (result.reply or "")
    assert "disk full" in (result.reply or "")


# ----------------------------------------------------------------------
# _parse_flags
# ----------------------------------------------------------------------


def test_parse_flags_empty():
    assert _parse_flags("") == {}


def test_parse_flags_long_with_value():
    assert _parse_flags("--provider minmax --model M3") == {
        "provider": "minmax",
        "model": "M3",
    }


def test_parse_flags_short_with_value():
    # `-new` consumes as boolean because `--key` starts with `-`
    assert _parse_flags("-new --key sk") == {"new": True, "key": "sk"}


def test_parse_flags_boolean():
    assert _parse_flags("-new -default") == {"new": True, "default": True}


def test_parse_flags_mixed_long_dash_to_underscore():
    out = _parse_flags("--base-url https://x --api-key sk")
    assert out == {"base_url": "https://x", "api_key": "sk"}


def test_parse_flags_does_not_consume_flag_as_value():
    out = _parse_flags("--foo -bar")
    assert out == {"foo": True, "bar": True}