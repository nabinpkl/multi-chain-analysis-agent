import { create as createStore } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import { namespacedStoreName } from "@/lib/session";

/**
 * Per-developer codex-runtime model + reasoning-effort override.
 * Pinned via the builder view's `ModelsPanel` codex section, persisted
 * to localStorage so the choice survives reload. Honored only on
 * turns where the active runtime is `AGENT_RUNTIME_CODEX`;
 * pydantic-ai turns ignore the wire field entirely.
 *
 * Three-tier fallback when both fields are empty:
 *   1. (this store empty) → 2. `CODEX_PRIMARY_MODEL` /
 *   `CODEX_REASONING_EFFORT` env on agent-service →
 *   3. codex-cli's own internal default.
 *
 * Wire shape is `multichain.wire.agent.v1.CodexOverride`
 * (`{model_id, reasoning_effort}`). The hook in
 * `use-agent-stream.ts` reads this store and stamps the matching
 * field onto every outgoing `AgentRequest` when at least one of
 * the two strings is non-empty.
 *
 * Lives separately from `useLlmOverride` because the codex panel is
 * a different shape: codex has no openrouter/gemini/local provider
 * triad (it always routes through codex-cli), and reasoning_effort
 * has no analog on the pydantic-ai side. Merging them would push a
 * codex-specific discriminator into every pydantic-ai turn for no
 * gain.
 */

export interface CodexOverrideStore {
  /** Codex-CLI model id (e.g. "gpt-5", "gpt-5-mini"). "" = use
   *  the env/cli default (cleared pin). */
  modelId: string;
  /** Codex-CLI reasoning effort ("low" | "medium" | "high"). "" =
   *  use env/cli default. */
  reasoningEffort: string;
  setModelId: (id: string) => void;
  setReasoningEffort: (effort: string) => void;
  reset: () => void;
}

export const useCodexOverride = createStore<CodexOverrideStore>()(
  persist(
    (set) => ({
      modelId: "",
      reasoningEffort: "",
      setModelId: (id) => set({ modelId: id }),
      setReasoningEffort: (effort) => set({ reasoningEffort: effort }),
      reset: () => set({ modelId: "", reasoningEffort: "" }),
    }),
    {
      name: namespacedStoreName("codex-override"),
      storage: createJSONStorage(() => localStorage),
      version: 1,
    },
  ),
);
