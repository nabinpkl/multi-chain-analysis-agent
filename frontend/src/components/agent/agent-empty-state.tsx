"use client";

import { SparklesIcon } from "lucide-react";

/**
 * Empty state shown when the agent panel opens with no active session.
 * Communicates capability, default frame, and how the user's view
 * informs the answer (per D-6).
 */
export function AgentEmptyState({ focusedAddr }: { focusedAddr: string | null }) {
  return (
    <div className="flex flex-col items-start gap-3 px-4 py-6 text-sm text-mca-muted">
      <div className="flex items-center gap-2">
        <SparklesIcon className="size-4 text-mca-text" />
        <span className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-text">
          ask the agent
        </span>
      </div>
      <p className="leading-relaxed text-xs">
        Ask a question about what you see in the live graph. The agent reads
        your current focus and selection as ground truth, so &ldquo;this
        wallet&rdquo; or &ldquo;these wallets&rdquo; resolve without
        ambiguity.
      </p>
      {focusedAddr ? (
        <p className="text-xs leading-relaxed text-mca-text">
          Currently focused:{" "}
          <span className="tabular-nums">{abbreviate(focusedAddr)}</span>
        </p>
      ) : (
        <p className="text-xs leading-relaxed">
          No focus yet. Click any wallet on the graph to set focus.
        </p>
      )}
      <ul className="text-xs leading-relaxed list-disc list-inside pl-1 space-y-1 opacity-80">
        <li>profile this wallet</li>
        <li>what role does this wallet play</li>
        <li>describe the largest community in the current window</li>
      </ul>
    </div>
  );
}

function abbreviate(addr: string): string {
  if (addr.length <= 10) return addr;
  return `${addr.slice(0, 4)}...${addr.slice(-4)}`;
}
