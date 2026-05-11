import { create as createStore } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import { namespacedStoreName } from "@/lib/session";
import { AgentRuntime } from "@/lib/wire/multichain/wire/agent/v1/session_pb";

/**
 * Per-developer runtime selector for the agent. Choices the user
 * sees in the builder view's `RuntimePanel`; persists to
 * localStorage so the choice survives reload. Production frontend
 * never renders the panel, so production traffic carries the
 * default (UNSPECIFIED on the wire, which the backend maps to
 * PYDANTIC_AI per the chunk 3 plan).
 *
 * Runtime is **locked per thread at creation**: the backend writes
 * `<thread_root>/threads/<thread_id>/runtime.json` on mint and 400s
 * any later turn whose `runtime` field disagrees. The `RuntimePanel`
 * disables the radios while a thread is open so the user can't
 * even attempt a runtime switch mid-conversation; clicking "new"
 * in `AgentSheet` clears the thread and re-enables the toggle.
 *
 * The hook in `use-agent-stream.ts` reads this store and stamps
 * `runtime` onto every outgoing `AgentRequest`.
 */

export interface RuntimeSelectorStore {
  /** Default `AGENT_RUNTIME_PYDANTIC_AI` keeps existing dev flows
   *  unchanged after this chunk lands. Empty / UNSPECIFIED would
   *  technically also work (backend defaults to pydantic-ai) but
   *  storing the explicit value is clearer in DevTools. */
  runtime: AgentRuntime;
  setRuntime: (runtime: AgentRuntime) => void;
  reset: () => void;
}

const DEFAULT_RUNTIME: AgentRuntime = AgentRuntime.PYDANTIC_AI;

export const useRuntimeSelector = createStore<RuntimeSelectorStore>()(
  persist(
    (set) => ({
      runtime: DEFAULT_RUNTIME,
      setRuntime: (runtime) => set({ runtime }),
      reset: () => set({ runtime: DEFAULT_RUNTIME }),
    }),
    {
      name: namespacedStoreName("runtime-selector"),
      storage: createJSONStorage(() => localStorage),
      version: 1,
    },
  ),
);
