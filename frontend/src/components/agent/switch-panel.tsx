"use client";

import { useState } from "react";
import { InfoIcon } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { PRESETS, useAgentSwitches } from "@/stores/use-agent-switches";

/**
 * Ablation switch panel. Each row is a behavior toggle paired with an
 * (i) info popover. Hovering the row text or the icon, or tapping the
 * icon on touch devices, surfaces a plain-language explanation of what
 * the switch does and what fails when it's off.
 *
 * Tooltips are deliberately user-facing: no implementation jargon,
 * no ship references. The implementation map lives in
 * `docs/architecture/switches.md`.
 */
export function SwitchPanel() {
  const switches = useAgentSwitches((s) => s.switches);
  const setSwitch = useAgentSwitches((s) => s.setSwitch);
  const applyPreset = useAgentSwitches((s) => s.applyPreset);

  return (
    <div className="border-b border-mca-border bg-mca-surface-raised px-4 py-3 space-y-3">
      <div className="space-y-2">
        <ToggleRow
          label="stay in role"
          tooltip="Keeps the agent focused on chain analysis. With this on it declines off-topic questions, won't write code, won't give financial advice, and won't pretend to be a different model. Turn it off to talk to the underlying LLM directly."
          on={switches.stay_in_role}
          onChange={(on) => setSwitch("stay_in_role", on)}
        />
        <ToggleRow
          label="don't fabricate"
          tooltip="Every number cited in an answer must trace back to data the agent actually fetched from the chain. With this on it can't invent values that look plausible but aren't real. Turn it off and the agent may make up numbers no tool actually returned."
          on={switches.dont_fabricate}
          onChange={(on) => setSwitch("dont_fabricate", on)}
        />
      </div>

      <div className="space-y-1.5">
        <p className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
          Cross check
        </p>
        <div className="space-y-2 pl-3 border-l border-mca-border">
          <ToggleRow
            label="paraphrase coherence"
            tooltip="A second pass reads the agent's prose and flags places where the words don't fit the data chip next to them (for example, 'a lot' next to a chip showing 1). Advisory only: it shows in the trace, doesn't block answers."
            on={switches.cross_check.paraphrase_aware_match}
            onChange={(on) =>
              setSwitch("cross_check.paraphrase_aware_match", on)
            }
          />
          <ToggleRow
            label="ground-truth match"
            tooltip="Re-checks every cited number directly against the live database, not just against what the agent's own tool call returned. Catches the case where a tool gave back stale or wrong data. Not active yet."
            sublabel="coming soon"
            on={switches.cross_check.ground_truth_match}
            onChange={(on) => setSwitch("cross_check.ground_truth_match", on)}
          />
        </div>
      </div>

      <div className="space-y-2 pt-1 border-t border-mca-border">
        <ToggleRow
          label="don't repeat yourself"
          tooltip="When you ask about the same wallet again, the agent re-fetches the data and tells you only what changed since last time. Live data keeps moving, so re-stating the whole answer would hide real movement."
          on={switches.dont_repeat_yourself}
          onChange={(on) => setSwitch("dont_repeat_yourself", on)}
        />
      </div>

      <div className="space-y-1.5 pt-1 border-t border-mca-border">
        <p className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted pt-2">
          Presets
        </p>
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              onClick={() => applyPreset(p.id)}
              title={p.description}
              className="text-[0.6rem] tracking-wide normal-case text-mca-text border border-mca-border rounded px-2 py-1 hover:bg-mca-surface hover:text-mca-accent transition-colors"
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * One switch row: text label + (i) info popover + Switch.
 *
 * The (i) popover opens on hover (desktop) and on tap (mobile). The
 * label text is wrapped in `<label htmlFor>` so clicking it still
 * toggles the underlying switch; the (i) button stops propagation so
 * tapping the icon never accidentally flips the switch.
 */
function ToggleRow({
  label,
  sublabel,
  tooltip,
  on,
  onChange,
}: {
  label: string;
  sublabel?: string;
  tooltip: string;
  on: boolean;
  onChange: (on: boolean) => void;
}) {
  const [tooltipOpen, setTooltipOpen] = useState(false);
  const switchKey = `switch-${label.replace(/[^a-z0-9]/gi, "-")}`;
  return (
    <div className="flex items-start justify-between gap-3">
      <div
        className="min-w-0 flex items-start gap-1.5"
        onMouseEnter={() => setTooltipOpen(true)}
        onMouseLeave={() => setTooltipOpen(false)}
      >
        <label htmlFor={switchKey} className="min-w-0 cursor-pointer">
          <span className="block text-[0.7rem] text-mca-text leading-tight">
            {label}
          </span>
          {sublabel ? (
            <span className="block text-[0.55rem] uppercase tracking-[1px] text-mca-dim mt-0.5">
              {sublabel}
            </span>
          ) : null}
        </label>
        <Popover open={tooltipOpen} onOpenChange={setTooltipOpen}>
          <PopoverTrigger
            render={
              <button
                type="button"
                aria-label={`What does "${label}" mean?`}
                className="shrink-0 -m-1 p-1 rounded text-mca-muted hover:text-mca-text focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent transition-colors"
              >
                <InfoIcon className="h-3.5 w-3.5" />
              </button>
            }
          />
          <PopoverContent
            side="top"
            align="end"
            sideOffset={6}
            className="w-72 text-[0.7rem] leading-relaxed text-mca-text"
          >
            {tooltip}
          </PopoverContent>
        </Popover>
      </div>
      <Switch
        id={switchKey}
        checked={on}
        onCheckedChange={onChange}
        className="shrink-0"
      />
    </div>
  );
}
