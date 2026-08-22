import { useState } from "react";
import { ChevronDown, ChevronRight, Wrench, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { cn } from "@/lib/utils";

interface ToolCallCardProps {
  name: string;
  args: string;
  result?: string;
  durationMs?: number;
  isError?: boolean;
  inFlight?: boolean;
}

/**
 * One row in the assistant's tool-call trace. Collapsed by default
 * because a busy chat with 5 tool calls shouldn't push the actual
 * reply off-screen.
 */
export function ToolCallCard({
  name,
  args,
  result,
  durationMs,
  isError,
  inFlight,
}: ToolCallCardProps) {
  const [open, setOpen] = useState(false);

  // Status icon: spinner while running, check on success, X on error.
  const Icon = inFlight
    ? Loader2
    : isError
      ? XCircle
      : CheckCircle2;

  const statusClass = inFlight
    ? "text-muted-foreground animate-spin"
    : isError
      ? "text-destructive"
      : "text-emerald-600 dark:text-emerald-400";

  // Pretty-print JSON args if they look like JSON.
  let prettyArgs = args;
  try {
    if (args.trim().startsWith("{") || args.trim().startsWith("[")) {
      prettyArgs = JSON.stringify(JSON.parse(args), null, 2);
    }
  } catch {
    // leave raw
  }

  return (
    <div className="mt-1 overflow-hidden rounded-md border border-border bg-muted/40 text-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-muted/70"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        )}
        <Wrench className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="font-mono text-xs">{name}</span>
        <Icon className={cn("ml-auto h-3.5 w-3.5 shrink-0", statusClass)} />
        {durationMs !== undefined && !inFlight && (
          <span className="shrink-0 text-xs text-muted-foreground">{durationMs}ms</span>
        )}
      </button>
      {open && (
        <div className="border-t border-border bg-background/50 px-3 py-2 text-xs">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Arguments
          </div>
          <pre className="overflow-x-auto rounded bg-muted/30 p-2 font-mono">
            {prettyArgs || "{}"}
          </pre>
          {result !== undefined && (
            <>
              <div className="mb-1 mt-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                Result
              </div>
              <pre className="max-h-80 overflow-auto rounded bg-muted/30 p-2 font-mono">
                {result}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
