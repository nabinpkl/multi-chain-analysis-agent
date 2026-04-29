"use client";

import { cn } from "@/lib/utils";
import {
  useAnalyticsStore,
  type LouvainSource,
} from "@/stores/analytics";

/** Two-state segmented control matching the WINDOW button style. */
const SOURCES: readonly { value: LouvainSource; label: string }[] = [
  { value: "frontend", label: "FRONTEND" },
  { value: "backend", label: "BACKEND" },
];

export function LouvainSourceToggle() {
  const louvainSource = useAnalyticsStore((s) => s.louvainSource);
  const setLouvainSource = useAnalyticsStore((s) => s.setLouvainSource);
  return (
    <div>
      <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-2">
        louvain
      </div>
      <div className="flex gap-1 text-xs">
        {SOURCES.map((s) => (
          <button
            key={s.value}
            onClick={() => setLouvainSource(s.value)}
            className={cn(
              "px-2 py-1 border rounded text-[0.65rem] uppercase tracking-[1.5px] transition-colors",
              louvainSource === s.value
                ? "border-emerald-500 text-mca-text"
                : "border-mca-border text-mca-muted hover:text-mca-text hover:border-mca-muted",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>
      <div className="text-[0.6rem] text-mca-dim mt-1 leading-relaxed">
        backend offloads community detection from the main thread
      </div>
    </div>
  );
}
