import { useEffect, useRef, useState } from "react";
import { Send, Square } from "lucide-react";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

interface ComposerProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

/**
 * Bottom-of-screen input. Autogrows up to 8 rows so the chat area
 * stays in view; Enter sends, Shift+Enter inserts a newline (the
 * natural expectation for chat composers).
 */
export function Composer({ onSend, disabled }: ComposerProps) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // Auto-resize on content change.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [value]);

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue("");
  };

  return (
    <div className="border-t border-border bg-background/80 px-4 py-3 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div
        className={cn(
          "mx-auto flex max-w-3xl items-end gap-2 rounded-lg border border-border bg-card px-3 py-2 shadow-sm transition focus-within:ring-1 focus-within:ring-ring",
          disabled && "opacity-60",
        )}
      >
        <textarea
          ref={ref}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={disabled ? "等待回复..." : "发消息（Enter 发送，Shift+Enter 换行）"}
          disabled={disabled}
          className="min-h-[36px] flex-1 resize-none border-0 bg-transparent text-sm outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
        />
        <Button
          size="icon"
          onClick={submit}
          disabled={disabled || !value.trim()}
          aria-label="发送"
          className="h-9 w-9 shrink-0"
        >
          {disabled ? <Square className="h-4 w-4" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}
