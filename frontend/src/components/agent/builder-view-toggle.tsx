"use client";

import { useState } from "react";
import { Switch } from "@/components/ui/switch";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useAgentSwitches } from "@/stores/use-agent-switches";

/**
 * Header toggle that switches the agent sheet between customer-
 * only view (default) and dual view (panel + trace). Adjacent (i)
 * popover explains why the toggle exists; first-time visitors
 * land on the clean view and opt in.
 *
 * The (i) popover opens on hover (desktop) and on tap (mobile),
 * matching the pattern in `SwitchPanel`. Open state is controlled
 * locally so mouseenter/mouseleave on the trigger button can drive
 * the popover without losing the built-in click-to-toggle behavior.
 */
export function BuilderViewToggle() {
  const builderViewOn = useAgentSwitches((s) => s.builderViewOn);
  const setBuilderViewOn = useAgentSwitches((s) => s.setBuilderViewOn);
  const [tooltipOpen, setTooltipOpen] = useState(false);

  return (
    <div className="flex items-center gap-2">
      <label
        htmlFor="builder-view-toggle"
        className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted cursor-pointer"
      >
        builder view
      </label>
      <Switch
        id="builder-view-toggle"
        checked={builderViewOn}
        onCheckedChange={setBuilderViewOn}
      />
      <Popover open={tooltipOpen} onOpenChange={setTooltipOpen}>
        <PopoverTrigger
          onMouseEnter={() => setTooltipOpen(true)}
          onMouseLeave={() => setTooltipOpen(false)}
          className="text-[0.7rem] text-mca-muted hover:text-mca-text transition-colors w-4 h-4 inline-flex items-center justify-center border border-mca-border rounded-full focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent"
          aria-label="why does builder view exist?"
        >
          i
        </PopoverTrigger>
        <PopoverContent
          side="bottom"
          align="end"
          className="w-80 text-[0.7rem] leading-relaxed bg-mca-surface text-mca-text border-mca-border space-y-2"
        >
          <p className="font-medium text-mca-accent">
            This is a builder portfolio piece, not a product.
          </p>
          <p>
            Flip <span className="text-mca-accent">builder view</span> to see how
            the agent&apos;s gate works:
          </p>
          <ul className="list-disc list-inside space-y-1 text-mca-dim pl-1">
            <li>
              5 toggles for the guardrails we built (stay in role, don&apos;t
              fabricate, cross check sub-modes)
            </li>
            <li>
              6 presets that show the agent at different construction states
              (raw LLM → production)
            </li>
            <li>A trace panel showing exactly which guardrail caught what</li>
          </ul>
          <p className="text-mca-dim">
            In production, customers see only the clean view. Builder view
            exists so visitors can see the architecture work, not just take
            it on faith.
          </p>
        </PopoverContent>
      </Popover>
    </div>
  );
}
