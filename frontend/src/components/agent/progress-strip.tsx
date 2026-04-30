"use client";

import { Loader2Icon } from "lucide-react";

export interface ProgressEvent {
  phase: string;
  detail: string;
}

/**
 * Renders the current Progress event above the claim list. v0 surfaces
 * just the most recent phase + detail; ship 4+ may show a fuller
 * timeline. Only visible while the loop is in flight.
 */
export function ProgressStrip({
  current,
  active,
}: {
  current: ProgressEvent | null;
  active: boolean;
}) {
  if (!active && !current) return null;
  const text = current
    ? formatPhase(current.phase, current.detail)
    : "preparing…";
  return (
    <div className="px-4 py-2 border-b border-mca-border bg-mca-bg flex items-center gap-2 text-xs text-mca-muted">
      {active ? (
        <Loader2Icon className="size-3 animate-spin text-mca-text" />
      ) : null}
      <span className="tabular-nums">{text}</span>
    </div>
  );
}

function formatPhase(phase: string, detail: string): string {
  switch (phase) {
    case "planning":
      return `planning… (${detail})`;
    case "tool_call":
      return `calling ${detail}…`;
    case "synthesizing":
      return `synthesizing (${detail})`;
    default:
      return `${phase}: ${detail}`;
  }
}
