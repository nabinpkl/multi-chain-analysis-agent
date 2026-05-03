import { create as createStore } from "zustand";
import { create } from "@bufbuild/protobuf";

import {
  AgentSwitchesSchema,
  CrossCheckSwitchesSchema,
  type AgentSwitches,
} from "@/lib/wire/multichain/wire/agent/v1/switches_pb";

/**
 * Ship 3.5 ablation switches + Ship 4 dontRepeatYourself + Ship 5a
 * citation-discipline gate refactor + builder-view toggle.
 *
 * Each switch is a behavior contract; flipping it off turns off
 * the corresponding guardrail or capability. See
 * `docs/architecture/switches.md`.
 *
 * Ship 5a removed `text_match` (regex-on-prose factuality check;
 * brittle on paraphrase + unicode). The structural placeholder +
 * value-compare gates under `dontFabricate` carry the load-bearing
 * factuality role. `paraphraseAwareMatch` survives but is reframed
 * from "factuality" to "coherence" and is advisory in the merge.
 *
 * `builderViewOn` controls whether the dual-view UI is rendered
 * (panel + trace timeline) and drives the wire's `showTrace`
 * field so the backend skips emitting `GatePath` frames when
 * casual visitors aren't looking. Default: false; visitors land
 * on the clean customer view first.
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

function makeSwitches(opts: {
  stayInRole: boolean;
  dontFabricate: boolean;
  paraphraseAwareMatch: boolean;
  groundTruthMatch: boolean;
  dontRepeatYourself: boolean;
}): AgentSwitches {
  return create(AgentSwitchesSchema, {
    stayInRole: opts.stayInRole,
    dontFabricate: opts.dontFabricate,
    crossCheck: create(CrossCheckSwitchesSchema, {
      paraphraseAwareMatch: opts.paraphraseAwareMatch,
      groundTruthMatch: opts.groundTruthMatch,
    }),
    dontRepeatYourself: opts.dontRepeatYourself,
  });
}

export const PRESETS: PresetMeta[] = [
  {
    id: "raw-llm",
    label: "raw LLM",
    description:
      "Nothing on. Model is just an LLM (will write Python, name itself, fabricate values, repeat itself).",
    switches: makeSwitches({
      stayInRole: false,
      dontFabricate: false,
      paraphraseAwareMatch: false,
      groundTruthMatch: false,
      dontRepeatYourself: false,
    }),
  },
  {
    id: "agent-without-grounding",
    label: "agent without grounding",
    description:
      "Stay-in-role on. Now a domain agent that declines off-topic. Still can fabricate; still re-states everything on repeat.",
    switches: makeSwitches({
      stayInRole: true,
      dontFabricate: false,
      paraphraseAwareMatch: false,
      groundTruthMatch: false,
      dontRepeatYourself: false,
    }),
  },
  {
    id: "non-fabricating-agent",
    label: "non-fabricating agent",
    description:
      "Add don't-fabricate. Every chip in the claim's prose must resolve to a provenance entry, and every cited Number value must trace to real tool output.",
    switches: makeSwitches({
      stayInRole: true,
      dontFabricate: true,
      paraphraseAwareMatch: false,
      groundTruthMatch: false,
      dontRepeatYourself: false,
    }),
  },
  {
    id: "with-paraphrase-cross-check",
    label: "+ paraphrase coherence",
    description:
      "Add paraphrase-aware coherence advisory. LLM extractor surfaces prose-vs-citation drift in the trace; advisory only, doesn't drive wire verdict.",
    switches: makeSwitches({
      stayInRole: true,
      dontFabricate: true,
      paraphraseAwareMatch: true,
      groundTruthMatch: false,
      dontRepeatYourself: false,
    }),
  },
  {
    id: "with-dont-repeat-yourself",
    label: "+ don't repeat yourself (production)",
    description:
      "Add don't-repeat-yourself. On a repeat question, agent re-fetches and surfaces only what changed since the prior turn instead of restating the whole answer. Current production default.",
    switches: makeSwitches({
      stayInRole: true,
      dontFabricate: true,
      paraphraseAwareMatch: true,
      groundTruthMatch: false,
      dontRepeatYourself: true,
    }),
  },
  {
    id: "with-ground-truth",
    label: "+ ground-truth cross-check (future)",
    description:
      "Add ground-truth match. Re-queries database; not implemented yet (lands in ship 5b).",
    switches: makeSwitches({
      stayInRole: true,
      dontFabricate: true,
      paraphraseAwareMatch: true,
      groundTruthMatch: true,
      dontRepeatYourself: true,
    }),
  },
];

interface AgentSwitchesStore {
  /** Current switch state. Defaults reproduce the production preset. */
  switches: AgentSwitches;
  /** Builder-view toggle. False = customer-only single column. */
  builderViewOn: boolean;
  setBuilderViewOn: (on: boolean) => void;
  setSwitch: (key: SwitchKey, on: boolean) => void;
  applyPreset: (id: PresetId) => void;
}

export type SwitchKey =
  | "stayInRole"
  | "dontFabricate"
  | "crossCheck.paraphraseAwareMatch"
  | "crossCheck.groundTruthMatch"
  | "dontRepeatYourself";

const PRODUCTION_PRESET = PRESETS.find((p) => p.id === "with-dont-repeat-yourself")!;

export const useAgentSwitches = createStore<AgentSwitchesStore>((set) => ({
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
  // Reconstruct from explicit fields rather than spreading `s` (which
  // would carry the `$typeName` literal type and conflict with create's
  // init shape on the cross_check sub-message).
  const cross = s.crossCheck;
  const base = {
    stayInRole: s.stayInRole,
    dontFabricate: s.dontFabricate,
    dontRepeatYourself: s.dontRepeatYourself,
    crossCheck: create(CrossCheckSwitchesSchema, {
      paraphraseAwareMatch: cross?.paraphraseAwareMatch ?? false,
      groundTruthMatch: cross?.groundTruthMatch ?? false,
    }),
  };
  switch (key) {
    case "stayInRole":
      return create(AgentSwitchesSchema, { ...base, stayInRole: on });
    case "dontFabricate":
      return create(AgentSwitchesSchema, { ...base, dontFabricate: on });
    case "crossCheck.paraphraseAwareMatch":
      return create(AgentSwitchesSchema, {
        ...base,
        crossCheck: create(CrossCheckSwitchesSchema, {
          paraphraseAwareMatch: on,
          groundTruthMatch: cross?.groundTruthMatch ?? false,
        }),
      });
    case "crossCheck.groundTruthMatch":
      return create(AgentSwitchesSchema, {
        ...base,
        crossCheck: create(CrossCheckSwitchesSchema, {
          paraphraseAwareMatch: cross?.paraphraseAwareMatch ?? false,
          groundTruthMatch: on,
        }),
      });
    case "dontRepeatYourself":
      return create(AgentSwitchesSchema, { ...base, dontRepeatYourself: on });
  }
}
