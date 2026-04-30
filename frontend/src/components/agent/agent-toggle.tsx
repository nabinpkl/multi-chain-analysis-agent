"use client";

import { SparklesIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Trigger for the agent sheet. Lives in the page header. Keyboard
 * shortcut (`⌘K` / `Ctrl+K`) is registered globally in
 * `graph-page.tsx`; clicking this button does the same thing.
 */
export function AgentToggle({
  open,
  onToggle,
}: {
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={onToggle}
      aria-pressed={open}
      className="gap-2 text-[0.7rem] uppercase tracking-[1.5px]"
    >
      <SparklesIcon className="size-4" />
      <span>agent</span>
      <kbd className="hidden md:inline text-[0.6rem] tabular-nums opacity-70 border border-mca-border rounded px-1 py-0.5">
        ⌘K
      </kbd>
    </Button>
  );
}
