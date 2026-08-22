"""Browser-based adapter for aaagent.

`WebAdapter` is an `IMAdapter` that bridges the in-process EventBus
to a set of WebSocket clients served by the FastAPI app in
`server.py`. It does **not** open the network socket itself — that
is the uvicorn server's job. The adapter's responsibility is the
fan-out: every event the bus emits that the web UI cares about is
serialised and pushed to every connected client.

Events forwarded downstream (bus → WS):
    * `message_to_send`   → `{type: "message", role, content, ...}`
    * `stream_token`      → `{type: "stream_token", content}`
    * `tool_start`        → `{type: "tool_start", turn, tool_calls, ...}`
    * `tool_result`       → `{type: "tool_result", ...}`
    * `slash_reply`       → `{type: "slash_reply", reply}`
    * `slash_quit`        → `{type: "slash_quit"}`
    * `slash_session_switch` → `{type: "slash_session_switch", ...}`
    * `slash_unknown`     → `{type: "slash_unknown", text, command}`

Events accepted upstream (WS → bus):
    * `{type: "user_message", content, session_id?, chat_id?, user_id?}`
        → emitted as `message_received` on the bus
    * `{type: "slash", text}`
        → emitted as `slash_command` on the bus

Why a separate "fan-out hub" instead of subscribing in `server.py`?
`Application.run()` calls `adapter.start()` once, after the FastAPI
app is mounted in the same process. The adapter subscribes to the
bus; the server hands it a callback to use for every pushed frame.
This keeps the WebSocket lifecycle (one socket per browser tab)
distinct from the adapter lifecycle (one per Application).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aaagent.core.bus import EventBus
from aaagent.core.message import Message
from aaagent.core.plugin import IMAdapter

logger = logging.getLogger("aaagent.adapter.web")


class WebAdapter(IMAdapter):
    """Browser/WebSocket adapter. See module docstring."""

    name = "web"

    def __init__(self, config: dict[str, Any], bus: EventBus) -> None:
        super().__init__(config, bus)
        # The default session / chat / user used for messages from the
        # web UI. The frontend may override these via per-message
        # `session_id` / `chat_id` / `user_id` fields so multi-user
        # deployments can keep tabs separated even though they share
        # one browser surface.
        self._session_id = str(config.get("default_session_id", "web-default"))
        self._chat_id = str(config.get("default_chat_id", "web-default"))
        self._user_id = str(config.get("default_user_id", "web-user"))
        self._bus = bus
        self._running = False
        # Each registered push callback corresponds to one WebSocket
        # connection. The server registers itself on `start()`.
        self._pushers: list[asyncio.Queue[str]] = []
        self._pushers_lock = asyncio.Lock()
        # Default user_id is also stored on the bus context so any
        # tools that look up `logctx.current_user_id()` see the right
        # value (the scheduler plugin uses this for owner-scoped
        # schedule listing, for example).
        self._bus.on("message_to_send", self._on_message_to_send)
        self._bus.on("stream_token", self._on_stream_token)
        self._bus.on("tool_start", self._on_tool_start)
        self._bus.on("tool_result", self._on_tool_result)
        self._bus.on("slash_reply", self._on_slash_reply)
        self._bus.on("slash_quit", self._on_slash_quit)
        self._bus.on("slash_session_switch", self._on_slash_session_switch)
        self._bus.on("slash_unknown", self._on_slash_unknown)

    # ------------------------------------------------------------------
    # IMAdapter surface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        # The server is mounted by `register_cli_command` (see
        # `__init__.py`). It owns its own uvicorn loop; we just wait
        # here until `stop()` flips `_running`. Blocking on
        # `_stop_event` lets `Application.run()` shut us down cleanly
        # when Ctrl+C is hit.
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        try:
            await stop_event.wait()
        finally:
            self._running = False

    async def stop(self) -> None:
        if hasattr(self, "_stop_event"):
            self._stop_event.set()

    async def send(self, msg: Message) -> None:
        """Called by the bus handler chain when the agent produces a
        reply addressed to a web user. The reply is already in the
        `message_to_send` payload, so we just re-emit it through our
        normal fan-out path. This keeps `send()` symmetric with the
        IMAdapter contract."""
        await self._bus.emit("message_to_send", msg)

    # ------------------------------------------------------------------
    # Connection registry (server side)
    # ------------------------------------------------------------------

    async def register_pusher(self) -> asyncio.Queue[str]:
        """Server calls this when a new WebSocket connects.

        Each tab gets its own queue so backpressure on one slow client
        doesn't stall another. We cap it so a persistently slow tab
        cannot drain the process: when full, the newest frame drops
        (see `_broadcast`) and the client simply doesn't see it.
        A client that recovers will catch up on the next event.
        """
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        async with self._pushers_lock:
            self._pushers.append(q)
        return q

    async def unregister_pusher(self, q: asyncio.Queue[str]) -> None:
        async with self._pushers_lock:
            try:
                self._pushers.remove(q)
            except ValueError:
                pass

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._pushers:
            return
        frame = json.dumps(payload, ensure_ascii=False)
        # Snapshot first so we don't hold the lock during the put.
        async with self._pushers_lock:
            pushers = list(self._pushers)
        for q in pushers:
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop the frame for this client. A slow client should
                # not block the rest of the surface; if it recovers
                # the next event will catch it up.
                logger.warning(
                    "web: dropping frame for slow client (queue full)"
                )

    # ------------------------------------------------------------------
    # Inbound: WS frame → bus event
    # ------------------------------------------------------------------

    async def handle_inbound(self, frame: dict[str, Any]) -> None:
        """Server calls this with the parsed JSON frame from the
        WebSocket. Translates the wire-level shape to the bus events
        the rest of aaagent already understands."""
        msg_type = frame.get("type")
        if msg_type == "user_message":
            await self._handle_user_message(frame)
        elif msg_type == "slash":
            await self._handle_slash(frame)
        elif msg_type == "ping":
            # Heartbeat. No-op; the server replies with a pong frame
            # synchronously because it owns the socket.
            return
        else:
            logger.warning("web: ignoring unknown inbound frame type: %r", msg_type)

    async def _handle_user_message(self, frame: dict[str, Any]) -> None:
        content = str(frame.get("content") or "")
        if not content.strip():
            return
        msg = Message(
            session_id=str(
                frame.get("session_id") or self._session_id
            ),
            platform="web",
            chat_id=str(frame.get("chat_id") or self._chat_id),
            user_id=str(frame.get("user_id") or self._user_id),
            content=content,
            role="user",
        )
        await self._bus.emit("message_received", msg)

    async def _handle_slash(self, frame: dict[str, Any]) -> None:
        text = str(frame.get("text") or "")
        if not text.startswith("/"):
            return
        await self._bus.emit(
            "slash_command",
            {
                "text": text,
                "platform": "web",
                "session_id": self._session_id,
                "chat_id": self._chat_id,
                "user_id": self._user_id,
            },
        )

    # ------------------------------------------------------------------
    # Outbound: bus event → WS frame
    # ------------------------------------------------------------------

    async def _on_message_to_send(self, msg: Message) -> None:
        if msg.platform != "web" and msg.platform != "cli":
            # Other adapters handle their own fan-out.
            return
        await self._broadcast(
            {
                "type": "message",
                "role": msg.role,
                "content": msg.content or "",
                "session_id": msg.session_id,
                "chat_id": msg.chat_id,
                "message_id": msg.id,
            }
        )

    async def _on_stream_token(self, token: str) -> None:
        await self._broadcast({"type": "stream_token", "content": token})

    async def _on_tool_start(self, data: dict[str, Any]) -> None:
        if data.get("platform") not in ("web", "cli"):
            return
        await self._broadcast(
            {
                "type": "tool_start",
                "turn": data.get("turn"),
                "tool_calls": [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in data.get("tool_calls", [])
                ],
            }
        )

    async def _on_tool_result(self, data: dict[str, Any]) -> None:
        if data.get("platform") not in ("web", "cli"):
            return
        await self._broadcast(
            {
                "type": "tool_result",
                "tool_call_id": data.get("tool_call_id"),
                "tool_name": data.get("tool_name"),
                "arguments": data.get("arguments"),
                "result": data.get("result"),
                "duration_ms": data.get("duration_ms"),
                "turn": data.get("turn"),
            }
        )

    async def _on_slash_reply(self, payload: dict[str, Any]) -> None:
        if payload.get("platform") not in ("web", "cli"):
            return
        await self._broadcast(
            {"type": "slash_reply", "reply": payload.get("reply", "")}
        )

    async def _on_slash_quit(self, payload: dict[str, Any]) -> None:
        if payload.get("platform") not in ("web", "cli"):
            return
        await self._broadcast({"type": "slash_quit"})

    async def _on_slash_session_switch(self, payload: dict[str, Any]) -> None:
        if payload.get("platform") not in ("web", "cli"):
            return
        new_session = payload.get("new_session")
        if new_session:
            self._session_id = str(new_session)
        await self._broadcast(
            {
                "type": "slash_session_switch",
                "new_session": new_session,
            }
        )

    async def _on_slash_unknown(self, payload: dict[str, Any]) -> None:
        if payload.get("platform") not in ("web", "cli"):
            return
        await self._broadcast(
            {
                "type": "slash_unknown",
                "text": payload.get("text", ""),
                "command": payload.get("command", ""),
            }
        )


__all__ = ["WebAdapter"]
