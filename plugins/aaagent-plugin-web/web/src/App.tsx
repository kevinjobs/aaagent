import { useCallback, useMemo, useReducer, useState } from "react";
import { useWebSocket, type ServerFrame } from "@/hooks/useWebSocket";
import { ChatView, type ChatItem } from "@/components/ChatView";
import { Composer } from "@/components/Composer";
import { Moon, Sun, Bot } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Top-level state machine. We append chat items as the stream
 * unfolds; the same item is mutated in place while streaming so the
 * assistant message "grows" without flickering.
 *
 * Two pieces of state that don't belong in items:
 *   * `streaming`: true while we're between the first stream_token
 *     and the final `message` frame.
 *   * the active tool trace (kept per-turn because each
 *     `tool_start`/`tool_result` pair maps to a single tool call).
 */
interface State {
  items: ChatItem[];
  streaming: boolean;
}

type Action =
  | { type: "user"; text: string }
  | { type: "assistant_begin"; id: string }
  | { type: "stream_token"; chunk: string }
  | { type: "assistant_end"; content: string }
  | { type: "tool_start"; turn: number; tool_calls: { name: string; arguments: string }[] }
  | { type: "tool_result"; tool_call_id: string; tool_name: string; arguments: string; result: string; duration_ms: number }
  | { type: "slash_reply"; reply: string }
  | { type: "slash_unknown"; text: string }
  | { type: "slash_session_switch"; new_session: string | null }
  | { type: "reset" };

const initialState: State = { items: [], streaming: false };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "user":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: `u-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: "user",
            content: action.text,
          },
        ],
        streaming: true,
      };
    case "assistant_begin":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: action.id,
            role: "assistant",
            content: "",
            streaming: true,
          },
        ],
      };
    case "stream_token":
      // Append to the last item if it's a streaming assistant;
      // otherwise drop the token (it came out-of-order).
      if (state.items.length === 0) return state;
      const last = state.items[state.items.length - 1];
      if (last.role !== "assistant" || !last.streaming) return state;
      return {
        ...state,
        items: [
          ...state.items.slice(0, -1),
          { ...last, content: last.content + action.chunk },
        ],
      };
    case "assistant_end":
      // Replace the streaming placeholder with the canonical content
      // (handles the case where the LLM produced no tokens but did
      // emit a final `message` frame).
      if (state.items.length === 0) {
        return {
          ...state,
          items: [
            {
              id: `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
              role: "assistant",
              content: action.content,
            },
          ],
          streaming: false,
        };
      }
      const lastItem = state.items[state.items.length - 1];
      if (lastItem.role === "assistant" && lastItem.streaming) {
        return {
          ...state,
          items: [
            ...state.items.slice(0, -1),
            { ...lastItem, content: action.content, streaming: false },
          ],
          streaming: false,
        };
      }
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: "assistant",
            content: action.content,
          },
        ],
        streaming: false,
      };
    case "tool_start":
      // Attach the in-flight tool calls to the most recent assistant
      // message. We synthesise unique ids locally because the server
      // only gives us names + args.
      if (state.items.length === 0) return state;
      const head = state.items[state.items.length - 1];
      if (head.role !== "assistant") return state;
      const trace = head.toolTrace ?? [];
      const newTrace = [
        ...trace,
        ...action.tool_calls.map((tc) => ({
          id: `tc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          name: tc.name,
          args: tc.arguments,
          inFlight: true,
        })),
      ];
      return {
        ...state,
        items: [
          ...state.items.slice(0, -1),
          { ...head, toolTrace: newTrace },
        ],
      };
    case "tool_result":
      if (state.items.length === 0) return state;
      const headR = state.items[state.items.length - 1];
      if (headR.role !== "assistant" || !headR.toolTrace) return state;
      // The server doesn't tell us which trace entry this matches
      // (it does send tool_call_id but the start frame didn't), so
      // we update the most recent inFlight one as a best-effort.
      const updated = [...headR.toolTrace];
      const idx = updated.findIndex((t) => t.inFlight);
      if (idx >= 0) {
        updated[idx] = {
          ...updated[idx],
          result: action.result,
          durationMs: action.duration_ms,
          isError: action.result.startsWith("Error:") || action.result.toLowerCase().startsWith("error"),
          inFlight: false,
        };
      }
      return {
        ...state,
        items: [
          ...state.items.slice(0, -1),
          { ...headR, toolTrace: updated },
        ],
      };
    case "slash_reply":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: "system",
            content: action.reply,
          },
        ],
      };
    case "slash_unknown":
      // Derive the bare command from the typed text so we can show
      // "未知命令：/foo" instead of dumping the full /foo args.
      const cmd = (action.text || "").trim().split(/\s+/)[0] || action.text;
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: "system",
            content: `未知命令：${cmd}`,
          },
        ],
      };
    case "slash_session_switch":
      return {
        ...state,
        items: [
          ...state.items,
          {
            id: `s-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: "system",
            content: `已切换到会话：${action.new_session ?? "(未指定)"}`,
          },
        ],
      };
    case "reset":
      return initialState;
  }
}

function getWsUrl(): string {
  // Same-host WebSocket. The Vite dev proxy passes `/api` to the
  // backend, so this works in dev too without an env var.
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/ws`;
}

function getInitialTheme(): "light" | "dark" {
  const stored = localStorage.getItem("aaagent-web-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export default function App() {
  const wsUrl = useMemo(getWsUrl, []);
  const { status, send, onFrame } = useWebSocket(wsUrl);
  const [state, dispatch] = useReducer(reducer, initialState);
  const [theme, setTheme] = useState<"light" | "dark">(getInitialTheme);

  // Wire inbound frames to the reducer. We open a streaming placeholder
  // on the first token and close it on the final `message` frame.
  // `useWebSocket`'s `onFrame` returns a cleanup function we capture.
  useMemo(() => {
    let lastAssistantId: string | null = null;
    onFrame((frame: ServerFrame) => {
      switch (frame.type) {
        case "stream_token":
          if (lastAssistantId === null) {
            lastAssistantId = `a-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
            dispatch({ type: "assistant_begin", id: lastAssistantId });
          }
          dispatch({ type: "stream_token", chunk: frame.content });
          break;
        case "message":
          if (frame.role === "assistant") {
            dispatch({ type: "assistant_end", content: frame.content });
          } else if (frame.role === "user") {
            // echo — usually the agent echoing back what we sent; ignore
          } else {
            dispatch({ type: "assistant_end", content: frame.content });
          }
          lastAssistantId = null;
          break;
        case "tool_start":
          dispatch({
            type: "tool_start",
            turn: frame.turn,
            tool_calls: frame.tool_calls,
          });
          break;
        case "tool_result":
          dispatch({
            type: "tool_result",
            tool_call_id: frame.tool_call_id,
            tool_name: frame.tool_name,
            arguments: frame.arguments,
            result: frame.result,
            duration_ms: frame.duration_ms,
          });
          break;
        case "slash_reply":
          dispatch({ type: "slash_reply", reply: frame.reply });
          break;
        case "slash_unknown":
          dispatch({ type: "slash_unknown", text: frame.text });
          break;
        case "slash_session_switch":
          dispatch({ type: "slash_session_switch", new_session: frame.new_session });
          break;
        case "slash_quit":
          // No surface action — the chat keeps going on the server.
          break;
      }
    });
  }, [onFrame]);

  const toggleTheme = useCallback(() => {
    setTheme((cur) => {
      const next = cur === "dark" ? "light" : "dark";
      localStorage.setItem("aaagent-web-theme", next);
      return next;
    });
  }, []);

  const handleSend = useCallback(
    (text: string) => {
      if (text.startsWith("/")) {
        send({ type: "slash", text });
      } else {
        dispatch({ type: "user", text });
        send({ type: "user_message", content: text });
      }
    },
    [send],
  );

  return (
    <div className={theme === "dark" ? "dark" : ""} data-theme={theme}>
      <div className="flex h-full flex-col bg-background text-foreground">
        <header className="flex h-12 items-center gap-2 border-b border-border px-4">
          <Bot className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-semibold">aaagent</span>
          <span className="text-xs text-muted-foreground">web</span>
          <div className="ml-auto flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              onClick={toggleTheme}
              aria-label="切换主题"
              className="h-8 w-8"
            >
              {theme === "dark" ? (
                <Sun className="h-4 w-4" />
              ) : (
                <Moon className="h-4 w-4" />
              )}
            </Button>
          </div>
        </header>

        <ChatView items={state.items} isStreaming={state.streaming} status={status} />

        <Composer onSend={handleSend} disabled={status !== "open"} />
      </div>
    </div>
  );
}
