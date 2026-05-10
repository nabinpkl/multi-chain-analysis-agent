import { create as createStore } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

/**
 * Per-developer LLM provider override for each agent role. Pinned via
 * the builder view's Models section, persisted to localStorage so the
 * choice survives page reload. Empty `provider` for any role means
 * "use the production preset (env-driven OpenRouter)" on the backend.
 *
 * The backend wire shape is `multichain.wire.agent.v1.LlmOverride`
 * (`primary` / `policy` / `judge` each carrying a `RoleOverride` of
 * `{provider, modelId}`). The hook in `use-agent-stream.ts` reads
 * this store and stamps the matching field onto every outgoing
 * `AgentRequest`. Production frontend never renders the Models
 * panel, so the field stays empty in prod.
 */

export type ProviderId = "" | "openrouter" | "gemini" | "local";

export interface RoleOverride {
  /** "" = use production default for this role. */
  provider: ProviderId;
  /** Meaningful only when provider="local". Model id loaded in LM
   *  Studio (the dropdown in `ModelsPanel` populates this from the
   *  agent-service `/agent/local/models` proxy). */
  modelId: string;
}

export type Role = "primary" | "policy" | "judge";

export interface LlmOverrideStore {
  primary: RoleOverride;
  policy: RoleOverride;
  judge: RoleOverride;
  setOverride: (role: Role, override: RoleOverride) => void;
  reset: () => void;
}

const EMPTY: RoleOverride = { provider: "", modelId: "" };

export const useLlmOverride = createStore<LlmOverrideStore>()(
  persist(
    (set) => ({
      primary: EMPTY,
      policy: EMPTY,
      judge: EMPTY,
      setOverride: (role, override) => set(() => ({ [role]: override })),
      reset: () => set({ primary: EMPTY, policy: EMPTY, judge: EMPTY }),
    }),
    {
      name: "mca:llm-override",
      storage: createJSONStorage(() => localStorage),
      version: 1,
    },
  ),
);
