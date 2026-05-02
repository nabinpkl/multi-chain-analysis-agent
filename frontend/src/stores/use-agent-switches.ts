import { create } from "zustand";
import type { AgentSwitches } from "@/lib/generated/AgentSwitches";

/**
 * Ship 3.5 ablation switches + Ship 4 dont_repeat_yourself + Ship 5a
 * citation-discipline gate refactor + builder-view toggle.
 *
 * Each switch is a behavior contract; flipping it off turns off
 * the corresponding guardrail or capability. See
 * `docs/architecture/switches.md` for what each switch does and
 * which code paths currently realize it.
 *
 * Ship 5a removed `text_match` (regex-on-prose factuality check;
 * brittle on paraphrase + unicode). The structural placeholder +
 * value-compare gates under `dont_fabricate` carry the load-bearing
 * factuality role. `paraphrase_aware_match` survives but is reframed
 * from "factuality" to "coherence" and is advisory in the merge.
 *
 * `builderViewOn` controls whether the dual-view UI is rendered
 * (panel + trace timeline) and drives the wire's `show_trace`
 * field so the backend skips emitting `GatePath` frames when
 * casual visitors aren't looking. Default: false; visitors land
 * on the clean customer view first.
 *
 * Presets are switch combinations describing "kinds of agent" a
 * visitor can construct. Each preset adds one behavior on top of
 * the previous so the panel reads as a layered story.
 */

export type PresetId =
  | "raw-llm"
  | "agent-without-grounding"
  | "non-fabricating-agent"
  | "with-paraphrase-cross-check"
  | "with-dont-repeat-yourself"
  | "with-ground-truth";

export interface PresetMeta {
  id: PresetId;
  label: string;
  description: string;
  switches: AgentSwitches;
}

export const PRESETS: PresetMeta[] = [
  {
    id: "raw-llm",
    label: "raw LLM",
    description:
      "Nothing on. Model is just an LLM (will write Python, name itself, fabricate values, repeat itself).",
    switches: {
      stay_in_role: false,
      dont_fabricate: false,
      cross_check: {
        paraphrase_aware_match: false,
        ground_truth_match: false,
      },
      dont_repeat_yourself: false,
    },
  },
  {
    id: "agent-without-grounding",
    label: "agent without grounding",
    description:
      "Stay-in-role on. Now a domain agent that declines off-topic. Still can fabricate; still re-states everything on repeat.",
    switches: {
      stay_in_role: true,
      dont_fabricate: false,
      cross_check: {
        paraphrase_aware_match: false,
        ground_truth_match: false,
      },
      dont_repeat_yourself: false,
    },
  },
  {
    id: "non-fabricating-agent",
    label: "non-fabricating agent",
    description:
      "Add don't-fabricate. Every chip in the claim's prose must resolve to a provenance entry, and every cited Number value must trace to real tool output.",
    switches: {
      stay_in_role: true,
      dont_fabricate: true,
      cross_check: {
        paraphrase_aware_match: false,
        ground_truth_match: false,
      },
      dont_repeat_yourself: false,
    },
  },
  {
    id: "with-paraphrase-cross-check",
    label: "+ paraphrase coherence",
    description:
      "Add paraphrase-aware coherence advisory. LLM extractor surfaces prose-vs-citation drift in the trace; advisory only, doesn't drive wire verdict.",
    switches: {
      stay_in_role: true,
      dont_fabricate: true,
      cross_check: {
        paraphrase_aware_match: true,
        ground_truth_match: false,
      },
      dont_repeat_yourself: false,
    },
  },
  {
    id: "with-dont-repeat-yourself",
    label: "+ don't repeat yourself (production)",
    description:
      "Add don't-repeat-yourself. On a repeat question, agent re-fetches and surfaces only what changed since the prior turn instead of restating the whole answer. Current production default.",
    switches: {
      stay_in_role: true,
      dont_fabricate: true,
      cross_check: {
        paraphrase_aware_match: true,
        ground_truth_match: false,
      },
      dont_repeat_yourself: true,
    },
  },
  {
    id: "with-ground-truth",
    label: "+ ground-truth cross-check (future)",
    description:
      "Add ground-truth match. Re-queries database; not implemented yet (lands in ship 5b).",
    switches: {
      stay_in_role: true,
      dont_fabricate: true,
      cross_check: {
        paraphrase_aware_match: true,
        ground_truth_match: true,
      },
      dont_repeat_yourself: true,
    },
  },
];

interface AgentSwitchesStore {
  /** Current switch state. Defaults reproduce the production
   * preset (stay_in_role + dont_fabricate + paraphrase
   * + dont_repeat_yourself; ground-truth off because it's a stub). */
  switches: AgentSwitches;
  /** Builder-view toggle. False = customer-only single column.
   * True = panel + dual columns + GatePath frames on wire. */
  builderViewOn: boolean;
  setBuilderViewOn: (on: boolean) => void;
  setSwitch: (key: SwitchKey, on: boolean) => void;
  applyPreset: (id: PresetId) => void;
}

export type SwitchKey =
  | "stay_in_role"
  | "dont_fabricate"
  | "cross_check.paraphrase_aware_match"
  | "cross_check.ground_truth_match"
  | "dont_repeat_yourself";

const PRODUCTION_PRESET = PRESETS.find((p) => p.id === "with-dont-repeat-yourself")!;

export const useAgentSwitches = create<AgentSwitchesStore>((set) => ({
  switches: PRODUCTION_PRESET.switches,
  builderViewOn: false,
  setBuilderViewOn: (builderViewOn) => set({ builderViewOn }),
  setSwitch: (key, on) =>
    set((s) => ({ switches: applySwitchKey(s.switches, key, on) })),
  applyPreset: (id) => {
    const preset = PRESETS.find((p) => p.id === id);
    if (!preset) return;
    set({ switches: preset.switches });
  },
}));

function applySwitchKey(
  s: AgentSwitches,
  key: SwitchKey,
  on: boolean,
): AgentSwitches {
  switch (key) {
    case "stay_in_role":
      return { ...s, stay_in_role: on };
    case "dont_fabricate":
      return { ...s, dont_fabricate: on };
    case "cross_check.paraphrase_aware_match":
      return {
        ...s,
        cross_check: { ...s.cross_check, paraphrase_aware_match: on },
      };
    case "cross_check.ground_truth_match":
      return { ...s, cross_check: { ...s.cross_check, ground_truth_match: on } };
    case "dont_repeat_yourself":
      return { ...s, dont_repeat_yourself: on };
  }
}
