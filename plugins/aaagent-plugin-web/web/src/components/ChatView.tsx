import { useEffect, useRef } from "react";
import { MessageBubble, type ToolTrace } from "./MessageBubble";
import { Loader2 } from "lucide-react";

export interface ChatItem {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  toolTrace?: ToolTrace[];
  streaming?: boolean;
}

interface ChatViewProps {
  items: ChatItem[];
  isStreaming: boolean;
  status: "connecting" | "open" | "closed" | "error";
}

/**
 * Scrollable message list. Auto-scrolls to the bottom on new content
 * unless the user has scrolled away (within ~80 px of the bottom) to
 * read something; in that case we leave them alone and show a
 * "jump to latest" pill.
 */
export function ChatView({ items, isStreaming, status }: ChatViewProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stuckToBottom = useRef(true);

  // Track scroll position: stuck-to-bottom iff we are within ~80px of the end.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stuckToBottom.current = distance < 80;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Auto-scroll on new content iff the user was already at the bottom.
  useEffect(() => {
    if (stuckToBottom.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [items, isStreaming]);

  return (
    <div className="relative flex-1 overflow-hidden">
      <div
        ref={scrollerRef}
        className="h-full overflow-y-auto px-4 py-6"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-4">
          {items.length === 0 && (
            <div className="mt-20 text-center text-sm text-muted-foreground">
              <p className="mb-2 text-base font-medium text-foreground">
                aaagent 已就绪
              </p>
              <p>向 aaagent 提问，或发送 /help 查看可用斜杠命令。</p>
            </div>
          )}
          {items.map((it) => (
            <MessageBubble
              key={it.id}
              role={it.role}
              content={it.content}
              toolTrace={it.toolTrace}
              streaming={it.streaming}
            />
          ))}
          {isStreaming && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground animate-fade-in">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>aaagent 正在回复…</span>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Status pill — bottom-left, only shows when not "open". */}
      {status !== "open" && (
        <div className="pointer-events-none absolute bottom-3 left-3">
          <div className="rounded-full border border-border bg-card/80 px-3 py-1 text-xs text-muted-foreground shadow-sm backdrop-blur">
            {status === "connecting" && "连接中…"}
            {status === "closed" && "连接已断开，正在重连"}
            {status === "error" && "连接错误，正在重连"}
          </div>
        </div>
      )}
    </div>
  );
}
