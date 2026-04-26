"use client";

import { useState } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface LiveIndicatorProps {
  active: boolean;
}

export function LiveIndicator({ active }: LiveIndicatorProps) {
  const [open, setOpen] = useState(false);

  return (
    <TooltipProvider delay={150}>
      <Tooltip open={open} onOpenChange={setOpen}>
        <TooltipTrigger
          render={
            <button
              type="button"
              aria-label="Stream connection status"
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
              <span className="text-[0.7rem] uppercase tracking-[1.5px] text-mca-dim">
                {active ? "Live" : "Idle"}
              </span>
            </button>
          }
        />
        <TooltipContent
          side="bottom"
          className="max-w-[260px] text-[0.7rem] leading-snug tracking-normal normal-case"
        >
          Raw edge stream from Solana ingester. Edges paint as transactions arrive.
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
