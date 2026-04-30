"use client";

import { useState } from "react";
import { AlertTriangleIcon, ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import { useAgentDiagnostics } from "@/hooks/use-agent-diagnostics";
import { cn } from "@/lib/utils";

/**
 * Persistent visibility for stubbed guardrails. Per the ship-1 plan:
 * silent stubs are silent bugs. This banner names every active stub,
 * shows hit counters, and identifies which ship promotes each. When
 * a stub is removed (its `register` call deleted), the entry
 * disappears here automatically.
 *
 * Always visible while the agent sheet is open; cannot be dismissed.
 * Click to expand/collapse the full list.
 */
export function StubBanner({ enabled }: { enabled: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const { diagnostics, error } = useAgentDiagnostics(enabled);

  if (!diagnostics && !error) {
    return (
      <div className="px-3 py-2 border-b border-mca-border text-[0.6rem] uppercase tracking-[1.5px] text-mca-dim">
        loading agent diagnostics…
      </div>
    );
  }
  if (error) {
    return (
      <div className="px-3 py-2 border-b border-mca-border text-[0.6rem] uppercase tracking-[1.5px] text-amber-500">
        diagnostics unreachable: {error}
      </div>
    );
  }
  const stubs = diagnostics?.stubs ?? [];
  const stubCount = stubs.length;

  return (
    <div className="border-b border-mca-border bg-amber-500/5">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-amber-500/10 transition-colors"
        aria-expanded={expanded}
      >
        <span className="flex items-center gap-2 text-[0.65rem] uppercase tracking-[1.5px] text-amber-500/90">
          <AlertTriangleIcon className="size-3" />
          stubs ({stubCount}) active
        </span>
        <span className="flex items-center gap-2 text-[0.6rem] text-amber-500/70 tabular-nums">
          {diagnostics?.provider}/{shortModel(diagnostics?.primary_model ?? "")}
          {expanded ? (
            <ChevronUpIcon className="size-3" />
          ) : (
            <ChevronDownIcon className="size-3" />
          )}
        </span>
      </button>
      {expanded ? (
        <div className="px-3 pb-3 space-y-2">
          {stubs.length === 0 ? (
            <p className="text-xs text-mca-muted">
              No stubs active. All guardrails are real.
            </p>
          ) : (
            stubs.map((s) => (
              <div
                key={s.name}
                className={cn(
                  "border border-amber-500/30 rounded p-2 space-y-1",
                  "bg-amber-500/5",
                )}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-[0.65rem] uppercase tracking-[1.5px] text-amber-500 tabular-nums">
                    {s.name}
                  </span>
                  <span className="text-[0.6rem] text-mca-muted tabular-nums">
                    hits: {s.hits}
                  </span>
                </div>
                <p className="text-[0.7rem] text-mca-text leading-relaxed">
                  {s.reason}
                </p>
                <p className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-dim">
                  promoted in ship {s.promoted_in_ship} · component {s.component}
                </p>
              </div>
            ))
          )}
          <div className="pt-1 text-[0.6rem] uppercase tracking-[1.5px] text-mca-dim">
            primitives registered: {diagnostics?.registered_primitives.join(", ")}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function shortModel(model: string): string {
  // "nvidia/nemotron-3-super-120b-a12b:free" -> "nemotron-3-super…:free"
  const parts = model.split("/");
  const tail = parts[parts.length - 1] ?? model;
  if (tail.length > 28) return `${tail.slice(0, 24)}…${tail.slice(-3)}`;
  return tail;
}
