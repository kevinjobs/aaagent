import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Wire shape we get from the aaagent web backend.
 *
 * Keep this in sync with `aaagent_plugin_web.adapter.WebAdapter`'s
 * outbound frame serialisation. TypeScript is our only safety net
 * here — adding a new outbound event should require a corresponding
 * `useReducer` case in `App.tsx`.
 */
export type ServerFrame =
  | { type: "message"; role: string; content: string; session_id: string; chat_id: string; message_id: string }
  | { type: "stream_token"; content: string }
  | { type: "tool_start"; turn: number; tool_calls: { name: string; arguments: string }[] }
  | { type: "tool_result"; tool_call_id: string; tool_name: string; arguments: string; result: string; duration_ms: number; turn: number }
  | { type: "slash_reply"; reply: string }
  | { type: "slash_quit" }
  | { type: "slash_session_switch"; new_session: string | null }
  | { type: "slash_unknown"; text: string; command?: string };

export type ClientFrame =
  | { type: "user_message"; content: string; session_id?: string; chat_id?: string; user_id?: string }
  | { type: "slash"; text: string }
  | { type: "ping" };

type Status = "connecting" | "open" | "closed" | "error";

export interface UseWebSocketReturn {
  status: Status;
  send: (frame: ClientFrame) => void;
  onFrame: (handler: (frame: ServerFrame) => void) => () => void;
}

/**
 * Auto-reconnecting WebSocket hook.
 *
 * Reconnects with exponential backoff (250 ms → 2 s, capped). A
 * 30-second `ping` keeps intermediate proxies (and the uvicorn
 * keep-alive) happy even when the chat is idle.
 */
export function useWebSocket(url: string): UseWebSocketReturn {
  const [status, setStatus] = useState<Status>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const handlersRef = useRef<Set<(f: ServerFrame) => void>>(new Set());
  const reconnectAttempts = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const connect = useCallback(() => {
    if (pingTimer.current) clearInterval(pingTimer.current);
    setStatus("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("open");
      reconnectAttempts.current = 0;
      // Keep-alive every 30s. Cleared on reconnect so StrictMode's
      // double-invoke and tab-backgrounding don't spawn runaway
      // ping loops.
      if (pingTimer.current) clearInterval(pingTimer.current);
      pingTimer.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, 30_000);
    };

    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ServerFrame;
        for (const h of handlersRef.current) {
          h(frame);
        }
      } catch (err) {
        // Server is supposed to send only JSON; if it doesn't,
        // log to console and keep the socket alive.
        // eslint-disable-next-line no-console
        console.warn("web: malformed frame from server", err);
      }
    };

    ws.onerror = () => {
      setStatus("error");
    };

    ws.onclose = () => {
      setStatus("closed");
      if (pingTimer.current) {
        clearInterval(pingTimer.current);
        pingTimer.current = null;
      }
      // Exponential backoff with cap.
      const delay = Math.min(2_000, 250 * 2 ** reconnectAttempts.current);
      reconnectAttempts.current += 1;
      reconnectTimer.current = setTimeout(connect, delay);
    };
  }, [url]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (pingTimer.current) clearInterval(pingTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const send = useCallback((frame: ClientFrame) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(frame));
    } else {
      // eslint-disable-next-line no-console
      console.warn("web: dropping outbound frame, socket not open", frame);
    }
  }, []);

  const onFrame = useCallback((handler: (frame: ServerFrame) => void) => {
    handlersRef.current.add(handler);
    return () => {
      handlersRef.current.delete(handler);
    };
  }, []);

  return { status, send, onFrame };
}
