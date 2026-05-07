"use client";

import { useState } from "react";
import { InfoIcon } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  PRESETS,
  useAgentSwitches,
  type SwitchKey,
} from "@/stores/use-agent-switches";

/**
 * Ablation switch panel rendered inside builder view. Every wire
 * field on `AgentSwitches` is exposed as its own row so a human can
 * verify the negative path of each defense, channel, or cross-check
 * directly from the UI. Presets at the bottom snap the whole set to
 * a known good configuration.
 *
 * Tooltips are deliberately user-facing: no implementation jargon,
 * no ship references. The implementation map lives in
 * `docs/architecture/switches.md`.
 */
export function SwitchPanel() {
  const switches = useAgentSwitches((s) => s.switches);
  const setSwitch = useAgentSwitches((s) => s.setSwitch);
  const applyPreset = useAgentSwitches((s) => s.applyPreset);

  const role = switches.stayInRole;
  const cross = switches.crossCheck;
  const channels = switches.channels;

  return (
    <div className="border-b border-mca-border bg-mca-surface-raised px-4 py-3 space-y-4">
      <Group title="Stay in role">
        <ToggleRow
          label="chat-template spoofing"
          tooltip="Boundary rail that rejects chat-template tokens (like </user> or [INST]) before they reach the agent. With this off, hostile inputs can fake a system message."
          on={role?.defendChatTemplateSpoofing ?? false}
          onChange={(on) =>
            setSwitch("stayInRole.defendChatTemplateSpoofing", on)
          }
        />
        <ToggleRow
          label="constitution judge"
          tooltip="A second LLM checks every claim and narrative against the agent's constitution before it ships. With this off the gate is skipped and verdicts default to approved."
          on={role?.defendConstitutionJudge ?? false}
          onChange={(on) =>
            setSwitch("stayInRole.defendConstitutionJudge", on)
          }
        />
        <ToggleRow
          label="persona swap"
          tooltip="Prompt-side rule that tells the agent to ignore 'pretend you are X' / 'play this character' jailbreaks. With this off the agent may adopt the requested persona."
          on={role?.defendPersonaSwap ?? false}
          onChange={(on) => setSwitch("stayInRole.defendPersonaSwap", on)}
        />
        <ToggleRow
          label="decode and execute"
          tooltip="Prompt-side rule that refuses to decode encoded payloads (base64, hex, rot13) and run them. With this off the agent may follow instructions hidden inside encoded text."
          on={role?.defendDecodeAndExecute ?? false}
          onChange={(on) =>
            setSwitch("stayInRole.defendDecodeAndExecute", on)
          }
        />
        <ToggleRow
          label="identity reveal"
          tooltip="Prompt-side rule that keeps the agent from naming the underlying LLM if asked 'what model are you'. With this off the agent will say."
          on={role?.defendIdentityReveal ?? false}
          onChange={(on) => setSwitch("stayInRole.defendIdentityReveal", on)}
        />
        <ToggleRow
          label="off-domain refusal"
          tooltip="Prompt-side rule that declines anything outside chain analysis (writing code, financial advice, general chat). With this off the agent will answer off-topic questions."
          on={role?.defendOffDomain ?? false}
          onChange={(on) => setSwitch("stayInRole.defendOffDomain", on)}
        />
        <ToggleRow
          label="memo injection"
          tooltip="Prompt-side rule that treats anything inside <external_data> blocks as data, never instructions. The boundary still wraps the data even with this off; this only removes the model-side defense-in-depth."
          on={role?.defendMemoInjection ?? false}
          onChange={(on) => setSwitch("stayInRole.defendMemoInjection", on)}
        />
      </Group>

      <Group title="Grounding">
        <ToggleRow
          label="don't fabricate"
          tooltip="Every number cited in an answer must trace back to data the agent actually fetched from the chain. With this off the agent may invent values that no tool returned."
          on={switches.dontFabricate}
          onChange={(on) => setSwitch("dontFabricate", on)}
        />
        <ToggleRow
          label="don't repeat yourself"
          tooltip="When you ask about the same wallet again, the agent re-fetches the data and tells you only what changed since last time. Live data keeps moving, so re-stating would hide real movement."
          on={switches.dontRepeatYourself}
          onChange={(on) => setSwitch("dontRepeatYourself", on)}
        />
      </Group>

      <Group title="Cross check">
        <ToggleRow
          label="paraphrase coherence"
          tooltip="A second pass reads the prose and flags places where the words don't fit the cited chip values (for example, 'a lot' next to a chip showing 1). Advisory only: shows in the trace, doesn't block answers."
          on={cross?.paraphraseAwareMatch ?? false}
          onChange={(on) => setSwitch("crossCheck.paraphraseAwareMatch", on)}
        />
        <ToggleRow
          label="ground-truth match"
          tooltip="Re-checks every cited number directly against the live database, not just against what the agent's own tool call returned. Catches stale or wrong tool data."
          sublabel="coming soon"
          on={cross?.groundTruthMatch ?? false}
          onChange={(on) => setSwitch("crossCheck.groundTruthMatch", on)}
        />
      </Group>

      <Group title="Channels">
        <ToggleRow
          label="narrative output"
          tooltip="The free-form prose channel. With this off the loop driver replaces the narrative text with empty before the SSE frame is emitted; the model still generates it but no consumer sees it. Use to test whether the agent can carry a turn through claim chips alone."
          on={channels?.narrativeOutputEnabled ?? false}
          onChange={(on) => setSwitch("channels.narrativeOutputEnabled", on)}
        />
        <ToggleRow
          label="external text input"
          tooltip="The wrapped <external_data> input channel. With this off, primitive tool outputs are sanitized before reaching the agent: free-text fields outside the constrained allowlist are replaced with placeholders. Forward-looking; no primitive emits free text yet."
          on={channels?.externalTextInputEnabled ?? false}
          onChange={(on) =>
            setSwitch("channels.externalTextInputEnabled", on)
          }
        />
      </Group>

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
 * Section heading + indented list. Used to group a family of
 * related switches (stay-in-role defenses, cross-check passes,
 * I/O channels) so the panel stays scannable as the surface grows.
 */
function Group({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <p className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
        {title}
      </p>
      <div className="space-y-2 pl-3 border-l border-mca-border">
        {children}
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

// Re-export the SwitchKey type so other consumers (tests, panels)
// can typecheck their setSwitch calls without re-importing the store.
export type { SwitchKey };
