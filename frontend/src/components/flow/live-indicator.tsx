"use client";

import { useState } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface LiveIndicatorProps {
  txPerSec: number;
  active: boolean;
}

const TIP =
  "Current pace of SOL transfers on Solana, averaged over the last ~30 seconds. Breathes up when the network is busy, down when it's quiet.";

export function LiveIndicator({ txPerSec, active }: LiveIndicatorProps) {
  const [open, setOpen] = useState(false);
  const rate = active ? formatRate(txPerSec) : "—";

  return (
    <TooltipProvider delay={150}>
      <Tooltip open={open} onOpenChange={setOpen}>
        <TooltipTrigger
          render={
            <button
              type="button"
              aria-label="Live transactions per second — what this means"
              onClick={() => setOpen((o) => !o)}
              className="inline-flex items-center gap-2 rounded-sm border border-mca-border bg-mca-surface/70 px-3 py-1.5 cursor-help focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent"
            >
              <span className="relative flex h-2 w-2">
                {active && (
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-mca-accent opacity-60" />
                )}
                <span
                  className={`relative inline-flex h-2 w-2 rounded-full ${
                    active ? "bg-mca-accent" : "bg-mca-muted"
                  }`}
                />
              </span>
              <span className="text-[0.7rem] uppercase tracking-[1.5px] text-mca-dim tabular-nums">
                {active ? "Live" : "Idle"} · {rate} tx/s
              </span>
            </button>
          }
        />
        <TooltipContent
          side="bottom"
          className="max-w-[260px] text-[0.7rem] leading-snug tracking-normal normal-case"
        >
          {TIP}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function formatRate(r: number): string {
  if (!isFinite(r) || r <= 0) return "0";
  if (r >= 100) return r.toFixed(0);
  if (r >= 10) return r.toFixed(1);
  return r.toFixed(2);
}
