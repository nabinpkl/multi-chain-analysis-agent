import { create as createStore } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { create } from "@bufbuild/protobuf";

import {
  AgentSwitchesSchema,
  ChannelSwitchesSchema,
  CrossCheckSwitchesSchema,
  StayInRoleSwitchesSchema,
  type AgentSwitches,
} from "@/lib/wire/multichain/wire/agent/v1/switches_pb";
import { namespacedStoreName } from "@/lib/session";

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
  // The single `stayInRole` UI toggle drives the two original
  // sub-defenses (boundary chat-template rejection + constitution
  // judge gate). The five per-prompt-rule defenses added in #36
  // phase 4 stay on regardless: their off-state changes the system
  // prompt content, which is article-only ablation and not
  // appropriate to expose on the production UI. Article-side
  // cases set those flags directly on the wire.
  return create(AgentSwitchesSchema, {
    stayInRole: create(StayInRoleSwitchesSchema, {
      defendChatTemplateSpoofing: opts.stayInRole,
      defendConstitutionJudge: opts.stayInRole,
      defendPersonaSwap: true,
      defendDecodeAndExecute: true,
      defendIdentityReveal: true,
      defendOffDomain: true,
    }),
    dontFabricate: opts.dontFabricate,
    crossCheck: create(CrossCheckSwitchesSchema, {
      paraphraseAwareMatch: opts.paraphraseAwareMatch,
      groundTruthMatch: opts.groundTruthMatch,
    }),
    dontRepeatYourself: opts.dontRepeatYourself,
    // Cockpit channels: production preset has every output channel
    // on. The narrative-output toggle (and the forward-looking
    // external-text-input toggle) are not currently surfaced on the
    // UI; they are wire-only ablation lanes for evals and the
    // article runner.
    channels: create(ChannelSwitchesSchema, {
      narrativeOutputEnabled: true,
      externalTextInputEnabled: true,
    }),
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
  // Composite stay-in-role flip (kept so presets and the legacy
  // top-row toggle still work). Drives both original sub-defenses
  // in lockstep.
  | "stayInRole"
  // Per-defense flips inside StayInRoleSwitches. Each one is a
  // single-bit ablation surface for the article-path runs and is
  // exposed individually in builder view so a human can verify
  // the negative path.
  | "stayInRole.defendChatTemplateSpoofing"
  | "stayInRole.defendConstitutionJudge"
  | "stayInRole.defendPersonaSwap"
  | "stayInRole.defendDecodeAndExecute"
  | "stayInRole.defendIdentityReveal"
  | "stayInRole.defendOffDomain"
  | "dontFabricate"
  | "crossCheck.paraphraseAwareMatch"
  | "crossCheck.groundTruthMatch"
  | "dontRepeatYourself"
  // Cockpit channel toggles. Each pairs with a deterministic
  // OTel attribute so the off-state is observable in the trace.
  | "channels.narrativeOutputEnabled"
  | "channels.externalTextInputEnabled";

const PRODUCTION_PRESET = PRESETS.find((p) => p.id === "with-dont-repeat-yourself")!;

/**
 * Persisted under a session-scoped name so a future per-user
 * isolation migration (auth) can derive the namespace from the
 * authenticated user id without losing in-flight state. Today the
 * session id is browser-stable via `localStorage["mca:sessionId"]`
 * (see `frontend/src/lib/session.ts`).
 *
 * `partialize` keeps only the state fields (drops the action fns,
 * which can't round-trip through JSON anyway). `merge` rebuilds
 * the `switches` proto wrapper from the parsed-JSON shape so the
 * hydrated value satisfies the typed `AgentSwitches` contract used
 * by `toJsonString(AgentSwitchesSchema, ...)` in `use-agent-stream`.
 */
export const useAgentSwitches = createStore<AgentSwitchesStore>()(
  persist(
    (set) => ({
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
    }),
    {
      name: namespacedStoreName("agent-switches"),
      storage: createJSONStorage(() => localStorage),
      version: 1,
      partialize: (state) => ({
        switches: state.switches,
        builderViewOn: state.builderViewOn,
      }),
      merge: (persisted, current) => {
        const p = (persisted as Partial<AgentSwitchesStore>) ?? {};
        // Rebuild the proto wrapper from the JSON-parsed shape so
        // the hydrated value is a valid `AgentSwitches` (carries
        // `$typeName`, sub-message wrappers, etc.). Falling back
        // to the production preset on any structural mismatch is
        // safer than crashing the app on a stale localStorage
        // shape; the user re-toggles if needed.
        let switches: AgentSwitches;
        try {
          switches = create(AgentSwitchesSchema, p.switches as AgentSwitches);
        } catch {
          switches = PRODUCTION_PRESET.switches;
        }
        return {
          ...current,
          switches,
          builderViewOn: p.builderViewOn ?? current.builderViewOn,
        };
      },
    },
  ),
);

function applySwitchKey(
  s: AgentSwitches,
  key: SwitchKey,
  on: boolean,
): AgentSwitches {
  // Reconstruct from explicit fields rather than spreading `s` (which
  // would carry the `$typeName` literal type and conflict with create's
  // init shape on sub-messages).
  const cross = s.crossCheck;
  const role = s.stayInRole;
  const channels = s.channels;
  // Carry every sub-field through unchanged when reconstructing the
  // sub-message so toggling another switch (e.g. dontFabricate) does
  // not silently reset role-defense fields.
  const carriedRole = {
    defendChatTemplateSpoofing: role?.defendChatTemplateSpoofing ?? false,
    defendConstitutionJudge: role?.defendConstitutionJudge ?? false,
    defendPersonaSwap: role?.defendPersonaSwap ?? false,
    defendDecodeAndExecute: role?.defendDecodeAndExecute ?? false,
    defendIdentityReveal: role?.defendIdentityReveal ?? false,
    defendOffDomain: role?.defendOffDomain ?? false,
  };
  const base = {
    stayInRole: create(StayInRoleSwitchesSchema, carriedRole),
    dontFabricate: s.dontFabricate,
    dontRepeatYourself: s.dontRepeatYourself,
    crossCheck: create(CrossCheckSwitchesSchema, {
      paraphraseAwareMatch: cross?.paraphraseAwareMatch ?? false,
      groundTruthMatch: cross?.groundTruthMatch ?? false,
    }),
    // Channel sub-message: carry every field through unchanged so
    // toggling another switch (e.g. dontFabricate) does not silently
    // reset cockpit lanes.
    channels: create(ChannelSwitchesSchema, {
      narrativeOutputEnabled: channels?.narrativeOutputEnabled ?? false,
      externalTextInputEnabled: channels?.externalTextInputEnabled ?? false,
    }),
  };
  // Helper: rebuild AgentSwitches with a single role sub-field
  // overridden, every other field carried through unchanged.
  const withRole = (overrides: Partial<typeof carriedRole>) =>
    create(AgentSwitchesSchema, {
      ...base,
      stayInRole: create(StayInRoleSwitchesSchema, {
        ...carriedRole,
        ...overrides,
      }),
    });
  // Helper: rebuild AgentSwitches with one channel field overridden.
  const withChannels = (overrides: {
    narrativeOutputEnabled?: boolean;
    externalTextInputEnabled?: boolean;
  }) =>
    create(AgentSwitchesSchema, {
      ...base,
      channels: create(ChannelSwitchesSchema, {
        narrativeOutputEnabled:
          overrides.narrativeOutputEnabled ??
          (channels?.narrativeOutputEnabled ?? false),
        externalTextInputEnabled:
          overrides.externalTextInputEnabled ??
          (channels?.externalTextInputEnabled ?? false),
      }),
    });
  switch (key) {
    case "stayInRole":
      // Composite UI toggle moves the two original sub-defenses in
      // lockstep. The five per-prompt-rule defenses each have their
      // own SwitchKey now and are not touched by the composite flip.
      return withRole({
        defendChatTemplateSpoofing: on,
        defendConstitutionJudge: on,
      });
    case "stayInRole.defendChatTemplateSpoofing":
      return withRole({ defendChatTemplateSpoofing: on });
    case "stayInRole.defendConstitutionJudge":
      return withRole({ defendConstitutionJudge: on });
    case "stayInRole.defendPersonaSwap":
      return withRole({ defendPersonaSwap: on });
    case "stayInRole.defendDecodeAndExecute":
      return withRole({ defendDecodeAndExecute: on });
    case "stayInRole.defendIdentityReveal":
      return withRole({ defendIdentityReveal: on });
    case "stayInRole.defendOffDomain":
      return withRole({ defendOffDomain: on });
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
    case "channels.narrativeOutputEnabled":
      return withChannels({ narrativeOutputEnabled: on });
    case "channels.externalTextInputEnabled":
      return withChannels({ externalTextInputEnabled: on });
  }
}
