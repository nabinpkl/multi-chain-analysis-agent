"use client";

import type { OverviewWindow } from "@/lib/api";

const OPTIONS: { value: OverviewWindow; label: string }[] = [
  { value: "15m", label: "15m" },
  { value: "1h", label: "1h" },
  { value: "6h", label: "6h" },
  { value: "24h", label: "24h" },
];

interface WindowSelectProps {
  value: OverviewWindow;
  onChange: (value: OverviewWindow) => void;
}

export function WindowSelect({ value, onChange }: WindowSelectProps) {
  return (
    <div
      className="inline-flex items-center gap-0 rounded-sm border border-mca-border bg-mca-surface/70 overflow-hidden"
      role="radiogroup"
      aria-label="Time window"
    >
      {OPTIONS.map((opt) => {
        const active = value === opt.value;
        return (
          <button
            key={opt.value}
            role="radio"
            aria-checked={active}
            onClick={() => onChange(opt.value)}
            className={`px-3 py-1.5 text-[0.7rem] uppercase tracking-[1.5px] transition-colors ${
              active
                ? "bg-mca-accent-dim text-mca-accent"
                : "text-mca-dim hover:text-mca-text"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
