import asyncio
import json

import pytest

from aaagent_plugin_feishu import (
    FeishuAdapter,
    _build_send_body,
    _looks_like_markdown,
    _resolve_env,
)
from aaagent.core.bus import EventBus
from aaagent.core.message import Message


def test_resolve_env_passthrough():
    assert _resolve_env("plain") == "plain"


def test_resolve_env_empty():
    assert _resolve_env("") == ""


def test_resolve_env_substitution(monkeypatch):
    monkeypatch.setenv("MY_FEISHU_KEY", "secret123")
    assert _resolve_env("${MY_FEISHU_KEY}") == "secret123"


def test_resolve_env_missing(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert _resolve_env("${NOPE}") == ""


def test_feishu_config_warning_when_missing(caplog):
    import logging

    bus = EventBus()
    with caplog.at_level(logging.WARNING):
        FeishuAdapter({"app_id": "", "app_secret": ""}, bus)
    assert any("Feishu adapter misconfigured" in r.getMessage() for r in caplog.records)


def test_feishu_remember_message_dedup():
    bus = EventBus()
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y"}, bus
    )  # missing config but we won't call start
    assert adapter._remember_message("om_1") is True
    assert adapter._remember_message("om_1") is False
    assert adapter._remember_message("om_2") is True


def test_feishu_remember_message_empty_id():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    assert adapter._remember_message("") is True
    assert adapter._remember_message("") is True  # empty always allowed


def test_feishu_truncate_long_message():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    long = "x" * 5000
    truncated = adapter._truncate_for_feishu(long)
    assert len(truncated) == 4000


def test_feishu_truncate_short_message_unchanged():
    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)
    short = "hello"
    assert adapter._truncate_for_feishu(short) == short


# ----------------------------------------------------------------------
# message_format dispatch
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hello world", False),
        ("plain prose with numbers 123", False),
        ("# Heading 1", True),
        ("## Heading 2", True),
        ("### H3", True),
        ("**bold text**", True),
        ("__also bold__", True),
        ("use `inline_code` here", True),
        ("```\ncode block\n```", True),
        ("[click](https://x.com)", True),
        ("> a quote line", True),
        ("- bullet item", True),
        ("+ plus bullet", True),
        ("* star bullet", True),
        ("1. numbered", True),
    ],
)
def test_looks_like_markdown(text, expected):
    assert _looks_like_markdown(text) is expected


def test_build_send_body_text():
    body = _build_send_body("oc_xxx", "hi", "text")
    assert body["receive_id"] == "oc_xxx"
    assert body["msg_type"] == "text"
    import json as _json

    assert _json.loads(body["content"]) == {"text": "hi"}


def test_build_send_body_markdown():
    body = _build_send_body("oc_xxx", "# Title\n\n**bold**", "markdown")
    assert body["receive_id"] == "oc_xxx"
    assert body["msg_type"] == "interactive"
    import json as _json

    card = _json.loads(body["content"])
    assert card["schema"] == "2.0"
    # Card v2: schema + body live at the root of the content payload.
    # A `card` wrapper is invalid and Feishu rejects it
    # ("unknown property, property: card").
    assert "card" not in card
    elements = card["body"]["elements"]
    assert len(elements) == 1
    assert elements[0]["tag"] == "markdown"
    assert elements[0]["content"] == "# Title\n\n**bold**"


def test_message_format_defaults_to_auto():
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, EventBus())
    assert adapter._message_format == "auto"


def test_message_format_explicit_value():
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y", "message_format": "markdown"},
        EventBus(),
    )
    assert adapter._message_format == "markdown"


def test_message_format_invalid_falls_back_to_auto(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        adapter = FeishuAdapter(
            {"app_id": "x", "app_secret": "y", "message_format": "bogus"},
            EventBus(),
        )
    assert adapter._message_format == "auto"
    assert any("feishu.message_format" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_send_text_format_uses_text_payload(monkeypatch):
    """message_format=text must send msg_type=text regardless of content."""
    import json as _json
    import time

    captured: list = []

    class _FakeResp:
        def __init__(self, code: int):
            self._code = code

        def json(self):
            return {"code": self._code, "msg": "ok"}

    class _FakeClient:
        is_closed = False

        async def post(self, url, params=None, headers=None, json=None):
            captured.append({"url": url, "json": json})
            return _FakeResp(0)

    bus = EventBus()
    adapter = FeishuAdapter(
        {
            "app_id": "x",
            "app_secret": "y",
            "message_format": "text",
        },
        bus,
    )
    adapter._tenant_token = "tk"
    adapter._token_expire_at = time.time() + 3600  # skip refresh
    adapter._http = _FakeClient()

    await adapter.send(
        Message(platform="feishu", chat_id="oc_xxx", content="# Title\n\n**bold**")
    )

    assert captured
    body = captured[0]["json"]
    assert body["msg_type"] == "text"
    assert _json.loads(body["content"]) == {
        "text": "# Title\n\n**bold**"
    }


@pytest.mark.asyncio
async def test_send_markdown_format_uses_card_v2():
    """message_format=markdown must send Card v2 with markdown element."""
    import json as _json
    import time

    captured: list = []

    class _FakeResp:
        def json(self):
            return {"code": 0, "msg": "ok"}

    class _FakeClient:
        is_closed = False

        async def post(self, url, params=None, headers=None, json=None):
            captured.append({"json": json})
            return _FakeResp()

    bus = EventBus()
    adapter = FeishuAdapter(
        {
            "app_id": "x",
            "app_secret": "y",
            "message_format": "markdown",
        },
        bus,
    )
    adapter._tenant_token = "tk"
    adapter._token_expire_at = time.time() + 3600
    adapter._http = _FakeClient()

    await adapter.send(
        Message(
            platform="feishu", chat_id="oc_xxx", content="**bold** and `code`"
        )
    )

    body = captured[0]["json"]
    assert body["msg_type"] == "interactive"
    card = _json.loads(body["content"])
    assert "card" not in card  # Card v2 has no `card` wrapper
    elements = card["body"]["elements"]
    assert elements[0]["tag"] == "markdown"
    assert elements[0]["content"] == "**bold** and `code`"


@pytest.mark.asyncio
async def test_send_auto_picks_markdown_when_md_detected():
    import time

    captured: list = []

    class _FakeResp:
        def json(self):
            return {"code": 0, "msg": "ok"}

    class _FakeClient:
        is_closed = False

        async def post(self, url, params=None, headers=None, json=None):
            captured.append({"json": json})
            return _FakeResp()

    bus = EventBus()
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y", "message_format": "auto"},
        bus,
    )
    adapter._tenant_token = "tk"
    adapter._token_expire_at = time.time() + 3600
    adapter._http = _FakeClient()

    await adapter.send(
        Message(platform="feishu", chat_id="oc_xxx", content="# Heading")
    )
    assert captured[0]["json"]["msg_type"] == "interactive"

    await adapter.send(
        Message(platform="feishu", chat_id="oc_xxx", content="plain prose")
    )
    assert captured[1]["json"]["msg_type"] == "text"


@pytest.mark.asyncio
async def test_feishu_slash_command_routes_via_bus(monkeypatch):
    """Slash-prefixed messages emit slash_command instead of message_received."""
    import asyncio
    from aaagent.core.bus import EventBus
    from aaagent.core.message import Message
    from aaagent_plugin_feishu import FeishuAdapter

    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)

    sent_to_message_received: list = []
    sent_to_slash_command: list = []

    async def on_msg(p):
        sent_to_message_received.append(p)

    async def on_slash(p):
        sent_to_slash_command.append(p)

    bus.on("message_received", on_msg)
    bus.on("slash_command", on_slash)

    sent: list = []

    async def fake_send(msg):
        sent.append(msg)

    adapter.send = fake_send  # type: ignore[method-assign]

    # Simulate Application-side reaction: reply via slash_reply → Feishu send
    await bus.emit(
        "slash_command",
        {
            "text": "/help",
            "platform": "feishu",
            "session_id": "feishu-oc_xxx",
            "chat_id": "oc_xxx",
        },
    )
    await bus.emit(
        "slash_reply",
        {
            "platform": "feishu",
            "session_id": "feishu-oc_xxx",
            "chat_id": "oc_xxx",
            "reply": "Available commands:\n  /help - ...",
            "suppressed": False,
        },
    )

    assert len(sent_to_slash_command) == 1
    assert sent_to_slash_command[0]["text"] == "/help"
    assert sent_to_message_received == []
    assert len(sent) == 1
    assert sent[0].content.startswith("Available commands")


@pytest.mark.asyncio
async def test_feishu_blacklisted_command_gets_not_supported_reply():
    """When /quit is blacklisted for feishu, the registry returns a
    suppressed reply and the adapter forwards it via send()."""
    from aaagent.core.bus import EventBus
    from aaagent.core.commands import (
        SlashCommandRegistry,
        SlashContext,
        register_builtins,
    )
    from aaagent_plugin_feishu import FeishuAdapter

    bus = EventBus()
    adapter = FeishuAdapter({"app_id": "x", "app_secret": "y"}, bus)

    sent: list = []

    async def fake_send(msg):
        sent.append(msg)

    adapter.send = fake_send  # type: ignore[method-assign]

    reg = SlashCommandRegistry()
    register_builtins(reg)
    ctx = SlashContext(platform="feishu", session_id="feishu-x", chat_id="x")
    result = await reg.handle("/quit", ctx, blacklist={"/quit"})

    if result.reply:
        await bus.emit(
            "slash_reply",
            {
                "platform": "feishu",
                "session_id": "feishu-x",
                "chat_id": "x",
                "reply": result.reply,
                "suppressed": result.suppressed,
            },
        )

    assert result.suppressed is True
    assert result.stop_adapter is False
    assert len(sent) == 1
    assert "feishu" in sent[0].content


# ----------------------------------------------------------------------
# pending indicator (slow-LLM hint)
# ----------------------------------------------------------------------


def _make_adapter_with_pending(**pending_overrides):
    """Build an adapter wired up for pending-indicator tests."""
    bus = EventBus()
    cfg = {
        "app_id": "x",
        "app_secret": "y",
        "pending_indicator": {"enabled": True, **pending_overrides},
    }
    adapter = FeishuAdapter(cfg, bus)
    # Avoid real HTTP for the indicator send / delete.
    adapter._http = _StubHttp()  # type: ignore[attr-defined]
    return adapter, bus


class _StubHttp:
    """Records sends / deletes and replies deterministically."""

    is_closed = False
    message_counter = 0

    def __init__(self) -> None:
        self.sent: list = []
        self.deleted: list = []

    async def post(self, url, params=None, headers=None, json=None):
        _StubHttp.message_counter += 1
        self.sent.append({"url": url, "json": json})
        # Mimic the real Feishu response shape.
        return _JsonResponse(
            {"code": 0, "msg": "ok", "data": {"message_id": f"om_{_StubHttp.message_counter}"}}
        )

    async def delete(self, url, headers=None):
        # DELETE /open-apis/im/v1/messages/<message_id>
        parts = url.rsplit("/", 1)
        msg_id = parts[-1]
        self.deleted.append(msg_id)
        return _JsonResponse({"code": 0, "msg": "ok"})


class _JsonResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


def _inbound_msg(chat_id: str = "oc_chat") -> Message:
    return Message(
        session_id=f"feishu-{chat_id}",
        platform="feishu",
        chat_id=chat_id,
        user_id="u_1",
        content="hi",
        role="user",
    )


def _reply_msg(chat_id: str = "oc_chat", text: str = "answer") -> Message:
    return Message(
        session_id=f"feishu-{chat_id}",
        platform="feishu",
        chat_id=chat_id,
        user_id="assistant",
        content=text,
        role="assistant",
    )


@pytest.mark.asyncio
async def test_pending_indicator_default_config_loads():
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y"}, EventBus()
    )
    assert adapter._pending_enabled is True
    assert adapter._pending_delay == 3.0
    assert adapter._pending_text == "🤔 思考中..."


@pytest.mark.asyncio
async def test_pending_indicator_can_be_disabled():
    bus = EventBus()
    adapter = FeishuAdapter(
        {"app_id": "x", "app_secret": "y", "pending_indicator": {"enabled": False}},
        bus,
    )
    assert adapter._pending_enabled is False
    # With the indicator disabled, message_received must not be wired.
    handlers = bus._handlers.get("message_received", [])  # type: ignore[attr-defined]
    assert not any(
        getattr(h, "__name__", "") == "_on_message_received_pending"
        for h in handlers
    )


@pytest.mark.asyncio
async def test_pending_indicator_fires_after_delay_and_deletes_on_reply():
    adapter, bus = _make_adapter_with_pending(delay_seconds=0.05, text="🤔…")
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg())

    # Yield the loop long enough for the timer to fire and the
    # indicator to be sent.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter._pending_message_ids:
            break
    assert adapter._pending_message_ids == {"oc_chat": "om_1"}
    assert adapter._http.sent[0]["json"]["msg_type"] == "text"  # type: ignore[attr-defined]
    assert "🤔" in json.loads(adapter._http.sent[0]["json"]["content"])["text"]  # type: ignore[attr-defined]

    # The reply arrives → indicator is deleted and the actual reply is sent.
    await bus.emit("message_to_send", _reply_msg())

    # Let the fire-and-forget delete settle.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter._http.deleted:  # type: ignore[attr-defined]
            break
    assert adapter._http.deleted == ["om_1"]  # type: ignore[attr-defined]
    assert "oc_chat" not in adapter._pending_message_ids
    # The real reply went out via the existing send handler.
    assert len(adapter._http.sent) == 2  # type: ignore[attr-defined]
    assert json.loads(adapter._http.sent[1]["json"]["content"])["text"] == "answer"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pending_indicator_is_cancelled_when_reply_lands_before_delay():
    adapter, bus = _make_adapter_with_pending(delay_seconds=0.5)
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg())
    # Reply well before the 0.5s timer expires.
    await asyncio.sleep(0.05)
    await bus.emit("message_to_send", _reply_msg())

    # Wait past the original timer to confirm nothing fires after cancellation.
    await asyncio.sleep(0.6)

    assert adapter._pending_message_ids == {}
    # The real reply went out; the pending indicator never fired.
    assert len(adapter._http.sent) == 1  # type: ignore[attr-defined]
    body_text = json.loads(adapter._http.sent[0]["json"]["content"])["text"]  # type: ignore[attr-defined]
    assert body_text == "answer"


@pytest.mark.asyncio
async def test_pending_indicator_per_chat_isolation():
    """Two concurrent chats each get their own timer and pending message."""
    adapter, bus = _make_adapter_with_pending(delay_seconds=0.05)
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg("oc_a"))
    await bus.emit("message_received", _inbound_msg("oc_b"))

    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(adapter._pending_message_ids) == 2:
            break

    assert set(adapter._pending_message_ids) == {"oc_a", "oc_b"}
    assert adapter._pending_message_ids["oc_a"] != adapter._pending_message_ids["oc_b"]

    # Reply on chat A only — chat B's indicator must remain.
    await bus.emit("message_to_send", _reply_msg("oc_a"))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if "oc_a" not in adapter._pending_message_ids:
            break

    assert "oc_a" not in adapter._pending_message_ids
    assert "oc_b" in adapter._pending_message_ids

    # Cleanup the remaining timer before the fixture exits.
    await bus.emit("message_to_send", _reply_msg("oc_b"))
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_pending_indicator_uses_configured_text():
    adapter, bus = _make_adapter_with_pending(
        delay_seconds=0.05, text="⏳ generating answer…"
    )
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter._pending_message_ids:
            break

    body_text = json.loads(adapter._http.sent[0]["json"]["content"])["text"]  # type: ignore[attr-defined]
    assert body_text == "⏳ generating answer…"

    await bus.emit("message_to_send", _reply_msg())
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_pending_indicator_new_message_replaces_old_for_same_chat():
    """Two inbound messages on the same chat cancel the first timer."""
    adapter, bus = _make_adapter_with_pending(delay_seconds=0.5)
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg())
    await asyncio.sleep(0.05)
    # Second inbound arrives — the first timer is cancelled and a new
    # one starts. We then reply before the (replaced) timer can fire.
    await bus.emit("message_received", _inbound_msg())
    await asyncio.sleep(0.05)
    await bus.emit("message_to_send", _reply_msg())

    # Wait past the 2nd timer to confirm it was cancelled by the reply.
    await asyncio.sleep(0.55)

    assert adapter._pending_message_ids == {}
    # Only the real reply was sent — no pending indicator at all.
    assert len(adapter._http.sent) == 1  # type: ignore[attr-defined]
    body_text = json.loads(adapter._http.sent[0]["json"]["content"])["text"]  # type: ignore[attr-defined]
    assert body_text == "answer"


@pytest.mark.asyncio
async def test_pending_indicator_does_not_block_other_platforms():
    """A message from another platform must not start a Feishu timer."""
    adapter, bus = _make_adapter_with_pending(delay_seconds=0.05)
    adapter._tenant_token = "tk"
    adapter._token_expire_at = 1e18
    adapter._running = True

    await bus.emit("message_received", _inbound_msg("oc_chat"))  # feishu → armed
    await bus.emit(
        "message_received",
        Message(
            session_id="cli-1",
            platform="cli",
            chat_id="cli",
            user_id="u",
            content="x",
            role="user",
        ),
    )
    # Wait for the feishu indicator.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if adapter._pending_message_ids:
            break

    assert "oc_chat" in adapter._pending_message_ids
    # Only one pending entry (feishu); the cli inbound was ignored.
    assert len(adapter._pending_message_ids) == 1

    await bus.emit("message_to_send", _reply_msg())
    await asyncio.sleep(0.1)