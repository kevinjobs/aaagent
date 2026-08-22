import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Standard shadcn `cn()` helper: combines `clsx` (conditional classes)
 * with `tailwind-merge` (resolves conflicting Tailwind utilities so
 * later classes win, e.g. `cn("p-2", "p-4")` → `"p-4"`).
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
