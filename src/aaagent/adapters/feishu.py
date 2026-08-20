from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import httpx
import websockets

from aaagent.adapters.base import IMAdapter
from aaagent.core.bus import EventBus
from aaagent.core.message import Message

logger = logging.getLogger("aaagent.feishu")

FEISHU_DOMAIN = "https://open.feishu.cn"
GEN_ENDPOINT_URI = "/callback/ws/endpoint"
SEND_MESSAGE_URI = "/open-apis/im/v1/messages"
TENANT_TOKEN_URI = "/open-apis/auth/v3/tenant_access_token/internal"


class FeishuAdapter(IMAdapter):
    name = "feishu"

    def __init__(self, config: dict[str, Any], bus: EventBus) -> None:
        super().__init__(config, bus)

        app_id = config.get("app_id", "")
        if app_id.startswith("${") and app_id.endswith("}"):
            app_id = os.environ.get(app_id[2:-1], "")

        app_secret = config.get("app_secret", "")
        if app_secret.startswith("${") and app_secret.endswith("}"):
            app_secret = os.environ.get(app_secret[2:-1], "")

        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = config.get("domain", FEISHU_DOMAIN).rstrip("/")
        self._running = False
        self._stop_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_task: asyncio.Task | None = None
        self._tenant_token: str = ""
        self._token_expire_at: float = 0.0

        self.bus.on("message_to_send", self._on_message_to_send)

    async def start(self) -> None:
        if not self._app_id or not self._app_secret:
            logger.error("Feishu app_id or app_secret not configured")
            return

        self._running = True
        self._stop_event.clear()
        self._loop = asyncio.get_running_loop()

        await self._refresh_tenant_token()

        logger.info("Feishu adapter started, waiting for messages...")
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.debug("Feishu ws_task created: %s", self._ws_task)

        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            logger.info("Feishu adapter cancelled")
            raise
        finally:
            await self._stop()

    async def _stop(self) -> None:
        self._running = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("Feishu adapter stopped")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def send(self, msg: Message) -> None:
        chat_id = msg.chat_id
        if not chat_id:
            logger.error("Cannot send Feishu message: missing chat_id")
            return

        await self._ensure_token()
        if not self._tenant_token:
            logger.error("Feishu tenant token unavailable")
            return

        content = json.dumps({"text": msg.content})
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": content,
        }
        params = {"receive_id_type": "chat_id"}
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._domain}{SEND_MESSAGE_URI}",
                    params=params,
                    headers=headers,
                    json=body,
                )
                data = resp.json()
                if data.get("code") != 0:
                    logger.error(
                        "Feishu send message failed: code=%s msg=%s",
                        data.get("code"),
                        data.get("msg"),
                    )
        except Exception as e:
            logger.error("Feishu send message error: %s", e)

    async def _ensure_token(self) -> None:
        if self._tenant_token and time.time() < self._token_expire_at - 60:
            return
        await self._refresh_tenant_token()

    async def _refresh_tenant_token(self) -> None:
        body = {"app_id": self._app_id, "app_secret": self._app_secret}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._domain}{TENANT_TOKEN_URI}",
                    json=body,
                )
                data = resp.json()
            if data.get("code") == 0:
                self._tenant_token = data.get("tenant_access_token", "")
                expire = data.get("expire", 7200)
                self._token_expire_at = time.time() + expire
                logger.info("Feishu tenant token refreshed, expires in %ss", expire)
            else:
                logger.error(
                    "Failed to get tenant token: code=%s msg=%s",
                    data.get("code"),
                    data.get("msg"),
                )
        except Exception as e:
            logger.error("Failed to get tenant token: %s", e)

    async def _get_ws_endpoint(self) -> tuple[str | None, int]:
        body = {"AppID": self._app_id, "AppSecret": self._app_secret}
        headers = {
            "locale": "zh",
            "User-Agent": "aaagent-feishu-adapter",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._domain}{GEN_ENDPOINT_URI}",
                    headers=headers,
                    json=body,
                )
                data = resp.json()
            if data.get("code") == 0:
                url = data.get("data", {}).get("URL")
                if url:
                    service_id = 0
                    try:
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(url).query)
                        if "service_id" in qs:
                            service_id = int(qs["service_id"][0])
                    except Exception:
                        pass
                    return url, service_id
            logger.error(
                "Failed to get WS endpoint: code=%s msg=%s",
                data.get("code"),
                data.get("msg"),
            )
        except Exception as e:
            logger.error("Failed to get WS endpoint: %s", e)
        return None, 0

    async def _ws_loop(self) -> None:
        reconnect_interval = 5
        while self._running and not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                logger.info("Feishu WS loop cancelled")
                break
            except Exception as e:
                logger.error("Feishu WS loop error: %s", e, exc_info=True)
            if self._stop_event.is_set():
                break
            logger.info("Reconnecting Feishu WS in %ss...", reconnect_interval)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=reconnect_interval)
            except asyncio.TimeoutError:
                pass

    async def _connect_and_listen(self) -> None:
        endpoint, service_id = await self._get_ws_endpoint()
        if not endpoint:
            await asyncio.sleep(5)
            return

        logger.info(
            "Connecting to Feishu WebSocket (service_id=%s)...", service_id
        )
        async with websockets.connect(endpoint, ping_interval=None, compression=None) as ws:
            logger.info("Connected to Feishu WebSocket")
            current_service_id = service_id
            ping_task = asyncio.create_task(self._ping_loop(ws, current_service_id))
            try:
                while self._running and not self._stop_event.is_set():
                    logger.debug("Feishu WS waiting for message...")
                    msg_raw = await ws.recv()
                    logger.info("Feishu WS recv: %d bytes", len(msg_raw))
                    await self._handle_ws_frame(msg_raw, ws)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _ping_loop(self, ws: Any, service_id: int) -> None:
        if not service_id:
            return
        try:
            ping_frame = _build_control_frame(service=service_id, msg_type="ping")
            await ws.send(ping_frame)
            logger.debug("Feishu WS initial ping sent")
        except Exception as e:
            logger.error("Feishu WS initial ping error: %s", e)
            return

        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.sleep(15)
                ping_frame = _build_control_frame(service=service_id, msg_type="ping")
                await ws.send(ping_frame)
                logger.debug("Feishu WS ping sent")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Feishu WS ping error: %s", e)
                break

    async def _handle_ws_frame(self, raw: bytes | str, ws: Any) -> None:
        if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
            preview = raw[:30].hex() if len(raw) > 30 else raw.hex()
            logger.info("Feishu WS frame received: %d bytes, head: %s", len(raw), preview)
        else:
            logger.info("Feishu WS frame received: %r", raw[:100] if isinstance(raw, (bytes, str)) else raw)
        try:
            if isinstance(raw, str):
                raw = raw.encode("utf-8", errors="ignore")

            if not raw:
                return

            frame = _parse_frame(raw)
            if frame is None:
                return

            method = frame.get("method", 0)
            headers = frame.get("headers", {}) or {}
            msg_type = headers.get("type", "")
            service = frame.get("service", 0)

            if method == 0:
                if msg_type == "ping":
                    pong_frame = _build_control_frame(service=service, msg_type="pong")
                    await ws.send(pong_frame)
                    logger.debug("Feishu WS pong sent")
                return

            if method != 1:
                return

            payload_bytes = frame.get("payload") or b""
            if not payload_bytes:
                return

            try:
                payload_str = payload_bytes.decode("utf-8", errors="ignore").strip()
                if not payload_str:
                    return
                envelope = json.loads(payload_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.debug("Feishu WS payload not JSON: %.120s", payload_bytes)
                return

            if msg_type == "card":
                return

            header = envelope.get("header", {}) or {}
            event_type = header.get("event_type", "")

            if event_type != "im.message.receive_v1":
                return

            payload = envelope.get("event", {}) or {}
            message = payload.get("message", {}) or {}
            sender = payload.get("sender", {}) or {}
            sender_id = sender.get("sender_id", {}) or {}

            if sender.get("sender_type") == "bot":
                return

            chat_type = message.get("chat_type", "p2p")
            mentions = message.get("mentions") or []

            if chat_type == "group":
                bot_mentioned = any(
                    (m.get("id") or {}).get("open_id")
                    and m.get("mentioned_type") == "bot"
                    for m in mentions
                )
                if not bot_mentioned:
                    return

            content_str = message.get("content", "")
            msg_type_inner = message.get("message_type", "text")
            text = self._extract_text(content_str, msg_type_inner)
            if not text.strip():
                return

            chat_id = message.get("chat_id", "")
            user_id = sender_id.get("open_id", "")
            session_id = f"feishu-{chat_id}"

            msg = Message(
                session_id=session_id,
                platform="feishu",
                chat_id=chat_id,
                user_id=user_id,
                content=text,
                role="user",
                raw={
                    "message_id": message.get("message_id"),
                    "chat_type": chat_type,
                },
            )

            await self.bus.emit("message_received", msg)

        except Exception as e:
            logger.error("Error handling Feishu WS frame: %s", e)

    async def _on_message_to_send(self, msg: Message) -> None:
        if msg.platform == "feishu":
            await self.send(msg)

    def _extract_text(self, content: str, msg_type: str) -> str:
        if msg_type != "text":
            return ""
        try:
            data = json.loads(content)
            text = data.get("text", "")
            text = re.sub(r"@_user_\d+\s*", "", text)
            return text.strip()
        except (json.JSONDecodeError, AttributeError):
            return content


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _parse_frame(data: bytes) -> dict[str, Any] | None:
    """Parse a Feishu WS Frame protobuf message.

    Frame schema (per lark_oapi/ws/pb/pbbp2_pb2.py and oapi-sdk-go ws/pbbp2.pb.go):
      1: uint64 SeqID
      2: uint64 LogID
      3: int32  Service
      4: int32  Method          (0=control/ping/pong, 1=data event/card)
      5: repeated Header { 1: string key, 2: string value }
      6: string PayloadEncoding
      7: string PayloadType
      8: bytes  Payload         (event envelope JSON for data frames)
      9: string LogIDNew
    """
    try:
        seq_id = 0
        log_id = 0
        service = 0
        method = 0
        headers: dict[str, str] = {}
        payload_encoding = ""
        payload_type = ""
        payload: bytes | None = None

        pos = 0
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07

            if wire_type == 0:
                value, pos = _read_varint(data, pos)
                if field_number == 1:
                    seq_id = value
                elif field_number == 2:
                    log_id = value
                elif field_number == 3:
                    service = value
                elif field_number == 4:
                    method = value
            elif wire_type == 2:
                length, pos = _read_varint(data, pos)
                value = data[pos:pos + length]
                pos += length
                if field_number == 5:
                    h = _parse_header(value)
                    if h is not None:
                        key, val = h
                        headers[key] = val
                elif field_number == 6:
                    payload_encoding = value.decode("utf-8", errors="ignore")
                elif field_number == 7:
                    payload_type = value.decode("utf-8", errors="ignore")
                elif field_number == 8:
                    payload = value
            else:
                break

        return {
            "seq_id": seq_id,
            "log_id": log_id,
            "service": service,
            "method": method,
            "headers": headers,
            "payload_encoding": payload_encoding,
            "payload_type": payload_type,
            "payload": payload,
        }
    except Exception as e:
        logger.debug("Failed to parse Feishu frame: %s", e)
        return None


def _parse_header(data: bytes) -> tuple[str, str] | None:
    """Parse a Feishu Header protobuf message: { 1: key, 2: value }."""
    try:
        key = ""
        value = ""
        pos = 0
        while pos < len(data):
            tag, pos = _read_varint(data, pos)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if wire_type != 2:
                break
            length, pos = _read_varint(data, pos)
            v = data[pos:pos + length]
            pos += length
            if field_number == 1:
                key = v.decode("utf-8", errors="ignore")
            elif field_number == 2:
                value = v.decode("utf-8", errors="ignore")
        return key, value
    except Exception:
        return None


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _encode_field_string(field_number: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_varint((field_number << 3) | 2) + _encode_varint(len(encoded)) + encoded


def _build_control_frame(service: int, msg_type: str) -> bytes:
    """Build a Feishu control frame (ping/pong).

    Per official NewPingFrame (oapi-sdk-go ws/model.go):
      Method  = 0 (FrameTypeControl)
      Service = service_id
      Headers = [{key="type", value=msg_type}]
    """
    header_bytes = _encode_field_string(1, "type") + _encode_field_string(2, msg_type)
    out = bytearray()
    if service:
        out += _encode_varint((3 << 3) | 0) + _encode_varint(service)
    out += _encode_varint((4 << 3) | 0) + _encode_varint(0)
    out += _encode_varint((5 << 3) | 2) + _encode_varint(len(header_bytes)) + header_bytes
    return bytes(out)