import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Bot, User } from "lucide-react";
import { cn } from "@/lib/utils";
import { ToolCallCard } from "./ToolCallCard";

export interface ToolTrace {
  id: string;
  name: string;
  args: string;
  result?: string;
  durationMs?: number;
  isError?: boolean;
  inFlight?: boolean;
}

interface MessageBubbleProps {
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  toolTrace?: ToolTrace[];
  streaming?: boolean;
}

/**
 * One bubble in the chat. User messages render right-aligned with
 * a filled neutral background; assistant messages render left-aligned
 * with no bubble (markdown is the focus). Tool trace rows sit just
 * below the assistant bubble as inline cards so the user can read
 * the answer and see what produced it.
 */
export function MessageBubble({
  role,
  content,
  toolTrace,
  streaming,
}: MessageBubbleProps) {
  if (role === "user") {
    return (
      <div className="flex items-start justify-end gap-2 animate-fade-in">
        <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-primary px-4 py-2 text-sm text-primary-foreground shadow-sm">
          {content}
        </div>
        <Avatar side="user" />
      </div>
    );
  }

  if (role === "system") {
    return (
      <div className="my-2 text-center text-xs text-muted-foreground animate-fade-in">
        {content}
      </div>
    );
  }

  if (role === "tool") {
    // Tool-only messages — historically unused; if a plugin emits one
    // we still surface it so nothing falls on the floor.
    return (
      <div className="flex items-start gap-2 animate-fade-in">
        <Avatar side="assistant" />
        <div className="max-w-[80%] text-sm text-muted-foreground">{content}</div>
      </div>
    );
  }

  // Assistant: markdown + optional tool trace beneath.
  return (
    <div className="flex items-start gap-2 animate-fade-in">
      <Avatar side="assistant" />
      <div className="min-w-0 max-w-[80%] space-y-1">
        <div
          className={cn(
            "prose prose-sm max-w-none dark:prose-invert",
            "prose-pre:bg-muted prose-pre:text-foreground prose-pre:border prose-pre:border-border",
            "prose-code:before:hidden prose-code:after:hidden",
            "prose-code:rounded prose-code:bg-muted prose-code:px-1.5 prose-code:py-0.5 prose-code:font-mono prose-code:text-[0.9em]",
            "prose-headings:font-semibold prose-headings:tracking-tight",
            "prose-a:text-foreground prose-a:underline",
          )}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[[rehypeHighlight, { detect: true }]]}
          >
            {content || ""}
          </ReactMarkdown>
          {streaming && (
            <span className="ml-0.5 inline-block h-4 w-1 translate-y-0.5 animate-pulse bg-foreground/60" />
          )}
        </div>
        {toolTrace && toolTrace.length > 0 && (
          <div className="space-y-1">
            {toolTrace.map((tc) => (
              <ToolCallCard
                key={tc.id}
                name={tc.name}
                args={tc.args}
                result={tc.result}
                durationMs={tc.durationMs}
                isError={tc.isError}
                inFlight={tc.inFlight}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Avatar({ side }: { side: "user" | "assistant" }) {
  const Icon = side === "user" ? User : Bot;
  const tone =
    side === "user"
      ? "bg-secondary text-secondary-foreground"
      : "bg-primary text-primary-foreground";
  return (
    <div
      className={cn(
        "flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
        tone,
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </div>
  );
}
