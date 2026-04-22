"use client";

import { useState } from "react";

import type { OverviewWindow } from "@/lib/api";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const OPTIONS: { value: OverviewWindow; label: string; secs: number }[] = [
  { value: "15m", label: "15m", secs: 15 * 60 },
  { value: "1h", label: "1h", secs: 60 * 60 },
  { value: "6h", label: "6h", secs: 6 * 60 * 60 },
  { value: "24h", label: "24h", secs: 24 * 60 * 60 },
];

interface WindowSelectProps {
  value: OverviewWindow;
  onChange: (value: OverviewWindow) => void;
  /** Seconds of data actually available. Buttons for windows larger
   * than this are disabled with a tooltip explaining why. */
  elapsedSecs: number;
}

function humanize(secs: number): string {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

export function WindowSelect({ value, onChange, elapsedSecs }: WindowSelectProps) {
  return (
    <TooltipProvider delay={150}>
      <div
        className="inline-flex items-center gap-0 rounded-sm border border-mca-border bg-mca-surface/70 overflow-hidden"
        role="radiogroup"
        aria-label="Time window"
      >
        {OPTIONS.map((opt) => {
          const active = value === opt.value;
          const disabled = elapsedSecs < opt.secs;

          if (!disabled) {
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
          }

          return (
            <DisabledWindowButton
              key={opt.value}
              label={opt.label}
              tip={`${opt.label} not available yet — only ${humanize(
                elapsedSecs,
              )} of data collected so far.`}
              checked={active}
            />
          );
        })}
      </div>
    </TooltipProvider>
  );
}

/**
 * Disabled window button with a base-ui Tooltip. Tooltip opens on:
 *   • hover (desktop)
 *   • focus (keyboard / screen reader)
 *   • tap (mobile) — we toggle open state explicitly because disabled
 *     buttons don't reliably fire focus on touch devices.
 */
function DisabledWindowButton({
  label,
  tip,
  checked,
}: {
  label: string;
  tip: string;
  checked: boolean;
}) {
  const [open, setOpen] = useState(false);

  return (
    <Tooltip open={open} onOpenChange={setOpen}>
      <TooltipTrigger
        render={
          <button
            role="radio"
            aria-checked={checked}
            aria-disabled
            onClick={() => setOpen((o) => !o)}
            className="px-3 py-1.5 text-[0.7rem] uppercase tracking-[1.5px] transition-colors text-mca-muted cursor-not-allowed"
          >
            {label}
          </button>
        }
      />
      <TooltipContent className="max-w-[220px] text-[0.7rem] tracking-normal normal-case">
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}
