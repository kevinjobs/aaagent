"""Tests for `aaagent_plugin_web`.

These cover the WebAdapter's event fan-out + the FastAPI app
factory's WebSocket round-trip. The Vite-built SPA isn't exercised
here — frontend tests live with the React project.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aaagent.core.bus import EventBus
from aaagent.core.message import Message
from aaagent_plugin_inmemorysession import InMemorySessionStore
from aaagent_plugin_web.adapter import WebAdapter
from aaagent_plugin_web.server import build_app


def _make_app_pair() -> tuple[TestClient, WebAdapter]:
    bus = EventBus()
    adapter = WebAdapter({}, bus)
    app = build_app(adapter)
    return TestClient(app), adapter


@pytest.mark.asyncio
async def test_adapter_fans_out_message_to_send_to_web_platform():
    """`message_to_send` events tagged `platform="web"` must reach
    registered push queues as a `message` frame."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()

    await adapter._bus.emit(
        "message_to_send",
        Message(
            session_id="s1",
            platform="web",
            chat_id="c1",
            user_id="u1",
            content="hello back",
            role="assistant",
        ),
    )

    # Allow the bus handlers (gather-ed) to drain.
    await asyncio.sleep(0)
    frame = json.loads(q.get_nowait())
    assert frame["type"] == "message"
    assert frame["role"] == "assistant"
    assert frame["content"] == "hello back"


@pytest.mark.asyncio
async def test_adapter_drops_message_to_send_for_non_web_platform():
    """A reply addressed at, say, `feishu` must NOT be fanned out
    to web clients — that would be a cross-platform leak."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()

    await adapter._bus.emit(
        "message_to_send",
        Message(
            session_id="s1",
            platform="feishu",
            chat_id="c1",
            user_id="u1",
            content="only for feishu",
            role="assistant",
        ),
    )
    await asyncio.sleep(0)
    with pytest.raises(asyncio.QueueEmpty):
        q.get_nowait()


@pytest.mark.asyncio
async def test_adapter_emits_message_received_on_user_message_frame():
    """Inbound `{type: user_message, content: ...}` must become a
    `message_received` event the rest of the system can react to."""
    client, adapter = _make_app_pair()
    captured: list[Message] = []
    adapter._bus.on("message_received", lambda m: captured.append(m) or asyncio.create_task(_noop()))

    async def _noop():
        pass

    await adapter.handle_inbound(
        {"type": "user_message", "content": "hi", "session_id": "alt"}
    )
    await asyncio.sleep(0)
    assert len(captured) == 1
    msg = captured[0]
    assert msg.platform == "web"
    assert msg.content == "hi"
    assert msg.session_id == "alt"


@pytest.mark.asyncio
async def test_adapter_emits_slash_command_on_slash_frame():
    """`{type: slash, text: "/help"}` becomes a slash_command event."""
    client, adapter = _make_app_pair()
    captured: list[dict] = []
    adapter._bus.on(
        "slash_command",
        lambda d: captured.append(d) or asyncio.create_task(_noop()),
    )

    async def _noop():
        pass

    await adapter.handle_inbound({"type": "slash", "text": "/model"})
    await asyncio.sleep(0)
    assert len(captured) == 1
    assert captured[0]["text"] == "/model"
    assert captured[0]["platform"] == "web"


def test_health_endpoint_returns_ok():
    client, _ = _make_app_pair()
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["platform"] == "web"


def test_index_returns_fallback_when_dist_missing(tmp_path):
    """When `web/dist/index.html` doesn't exist the server should
    still serve a usable landing page so users get a clear 'build
    me' message instead of a 404."""
    # Force the dist lookup to return None by patching the candidates.
    from aaagent_plugin_web import server as server_mod

    original = server_mod._find_dist_dir
    server_mod._find_dist_dir = lambda: None
    try:
        client, _ = _make_app_pair()
        r = client.get("/")
        assert r.status_code == 200
        assert "Frontend not built" in r.text
    finally:
        server_mod._find_dist_dir = original


def test_websocket_round_trip_emits_user_message():
    """End-to-end: client → WS → bus → message_received event.

    We use FastAPI's TestClient (which spins an in-process event
    loop) to talk to the WebSocket and assert the inbound frame
    reaches the bus.
    """
    client, adapter = _make_app_pair()
    captured: list[Message] = []
    ack = asyncio.Event()

    async def _capture(msg: Message) -> None:
        captured.append(msg)
        ack.set()

    adapter._bus.on("message_received", _capture)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "user_message", "content": "ping"}))

        # Wait briefly for the bus handler to run.
        for _ in range(50):
            if ack.is_set():
                break
            # Drain a beat so the WS receive loop completes.
            ws.receive_text() if False else None
            import time

            time.sleep(0.05)

    assert any(m.content == "ping" for m in captured), captured


def test_websocket_ping_frame_replies_with_pong():
    """Heartbeat frames must not be misrouted into the bus; the
    server replies synchronously with a pong so the client knows
    the socket is alive."""
    client, _ = _make_app_pair()
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        reply = json.loads(ws.receive_text())
        assert reply["type"] == "pong"


@pytest.mark.asyncio
async def test_push_queue_has_maxsize_to_prevent_oom_from_slow_client():
    """Regression: a persistently slow client (e.g. tab backgrounded
    by the browser) must not be able to drain the process. The
    per-connection push queue is capped; when it fills up the
    newest frame drops and the client simply doesn't see it."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()
    assert q.maxsize > 0, "queue must be capped to prevent slow-client OOM"


@pytest.mark.asyncio
async def test_broadcast_drops_newest_frame_when_queue_full():
    """When the push queue is full, `_broadcast` must drop the
    newest frame (not block) so the adapter's handler chain keeps
    moving and other clients still receive events."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()
    # Fill the queue to capacity.
    for _ in range(q.maxsize):
        q.put_nowait("filler")
    # Now try to broadcast a real frame.
    await adapter._bus.emit(
        "message_to_send",
        Message(
            session_id="s1",
            platform="web",
            chat_id="c1",
            user_id="u1",
            content="important",
            role="assistant",
        ),
    )
    await asyncio.sleep(0)
    # The queue must still be full, with "important" dropped.
    top = q.get_nowait()
    assert top == "filler"


@pytest.mark.asyncio
async def test_slash_unknown_frame_carrys_command_field():
    """`slash_unknown` must include the parsed `command` so the
    frontend can show '未知命令：/foo' rather than dumping the full
    '/foo arg' text. If the backend older version omits it, the
    frontend will fall back to splitting on whitespace."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()

    await adapter._bus.emit(
        "slash_unknown",
        {
            "platform": "web",
            "text": "/foo arg1",
            "command": "/foo",
        },
    )
    await asyncio.sleep(0)
    frame = json.loads(q.get_nowait())
    assert frame["type"] == "slash_unknown"
    assert frame["command"] == "/foo"


@pytest.mark.asyncio
async def test_handle_inbound_ignores_empty_user_message():
    """A user message with only whitespace must not reach the bus;
    emitting an empty message_to_send would pollute the session
    store and waste LLM tokens."""
    client, adapter = _make_app_pair()
    captured: list[Message] = []
    adapter._bus.on(
        "message_received",
        lambda m: captured.append(m) or asyncio.create_task(_noop()),
    )

    async def _noop():
        pass

    for payload in (
        {"type": "user_message", "content": ""},
        {"type": "user_message", "content": "   "},
        {"type": "user_message", "content": "\t\n"},
    ):
        await adapter.handle_inbound(payload)
        await asyncio.sleep(0)
    assert not captured


@pytest.mark.asyncio
async def test_broadcast_skips_platforms_other_than_web_and_cli():
    """Cross-platform event isolation: a reply destined for Feishu
    or a headless script must not appear in the browser."""
    client, adapter = _make_app_pair()
    q = await adapter.register_pusher()

    for platform in ("feishu", "cli-legacy", "mcp", "custom"):
        await adapter._bus.emit(
            "message_to_send",
            Message(
                session_id="s1",
                platform=platform,
                chat_id="c1",
                user_id="u1",
                content="leak?",
                role="assistant",
            ),
        )
        await asyncio.sleep(0)
    with pytest.raises(asyncio.QueueEmpty):
        q.get_nowait()


def test_websocket_rejects_malformed_json_frame():
    """Non-JSON inbound frames must close the socket cleanly with
    code 1003 (Unsupported Data) rather than raising an unhandled
    exception in the server loop."""
    client, _ = _make_app_pair()
    with pytest.raises(Exception):  # FastAPI.TestClient raises on 1003
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text("not-json-at-all")
            # Server should close with 1003.
            ws.receive_text()


# ---------- /api/session/messages: SPA hydration on refresh ----------


def _fake_app_with_store() -> tuple[TestClient, "object"]:
    """Build an app with a fake Application whose `_session_store` is
    seeded with one session. This lets us test the REST hydration
    endpoint without pulling in the full Application.
    """
    store = InMemorySessionStore(max_history=20)

    class FakeApp:
        _session_store = store

    adapter = WebAdapter({"default_session_id": "web-foo"}, EventBus())
    app = build_app(adapter, application=FakeApp)

    return TestClient(app), store


@pytest.mark.asyncio
async def test_session_messages_endpoint_returns_stored_messages():
    """The SPA calls `GET /api/session/messages` on mount to hydrate
    after a browser refresh. It must return every message from the
    default session so the local `items` reducer matches the
    backend's truth."""
    client, store = _fake_app_with_store()

    # Seed two messages (user + assistant) into the default session.
    await store.add_message(
        "web-foo",
        Message(
            session_id="web-foo",
            platform="web",
            chat_id="c1",
            user_id="u1",
            content="hi",
            role="user",
        ),
    )
    await store.add_message(
        "web-foo",
        Message(
            session_id="web-foo",
            platform="web",
            chat_id="c1",
            user_id="u1",
            content="hello back",
            role="assistant",
        ),
    )

    resp = client.get("/api/session/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "web-foo"
    assert len(data["messages"]) == 2
    assert data["messages"][0]["role"] == "user"
    assert data["messages"][0]["content"] == "hi"
    assert data["messages"][1]["role"] == "assistant"
    assert data["messages"][1]["content"] == "hello back"


@pytest.mark.asyncio
async def test_session_messages_defaults_to_adapter_default_session():
    """When the SPA doesn't specify a session_id (its default case),
    the endpoint must look up the adapter's `default_session_id` —
    which is the same session the frontend sends new messages into.
    """
    client, store = _fake_app_with_store()
    await store.add_message(
        "web-foo",
        Message(
            session_id="web-foo",
            platform="web",
            chat_id="c1",
            user_id="u1",
            content="default session msg",
            role="assistant",
        ),
    )
    # No `session_id` query param — must resolve to adapter's default.
    resp = client.get("/api/session/messages")
    data = resp.json()
    assert data["session_id"] == "web-foo"
    assert data["messages"][0]["content"] == "default session msg"


@pytest.mark.asyncio
async def test_session_messages_returns_empty_for_missing_session():
    """A session_id the store doesn't know about must return an empty
    list (200, not 404) so the frontend can safely render an empty
    chat without special-casing errors."""
    client, _ = _fake_app_with_store()
    resp = client.get("/api/session/messages?session_id=nonexistent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "nonexistent"
    assert data["messages"] == []


def test_session_messages_without_application_returns_empty():
    """If the FastAPI app is built without an Application reference
    (server.py `application=None`), the endpoint must not crash. It
    returns an empty payload so the SPA stays working when the
    REST APIs are unavailable."""
    adapter = WebAdapter({"default_session_id": "x"}, EventBus())
    client = TestClient(build_app(adapter, application=None))
    resp = client.get("/api/session/messages")
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


@pytest.mark.asyncio
async def test_sessions_list_endpoint_returns_all_sessions():
    """`GET /api/sessions` lists every stored session so the SPA can
    (in future) offer a session switcher. It must return metadata
    only — never the message contents."""
    client, store = _fake_app_with_store()

    for sid, role in [
        ("web-foo", "assistant"),
        ("web-bar", "assistant"),
    ]:
        await store.add_message(
            sid,
            Message(
                session_id=sid,
                platform="web",
                chat_id="c1",
                user_id="u1",
                content=f"msg in {sid}",
                role=role,
            ),
        )

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    ids = {s["id"] for s in data["sessions"]}
    assert "web-foo" in ids
    assert "web-bar" in ids
    # No `messages` key on any entry.
    assert all("messages" not in s for s in data["sessions"])


def test_health_endpoint_reports_default_session():
    """`/api/health` must include `default_session` so a client can
    confirm which session it will talk to without guessing."""
    adapter = WebAdapter({"default_session_id": "web-custom"}, EventBus())
    client = TestClient(build_app(adapter))
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_session"] == "web-custom"
