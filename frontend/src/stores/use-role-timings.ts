import { create } from "zustand";

/**
 * Per-role wall-time tally from the most recent completed turn.
 * Updated by `useAgentStream` when the SSE `Done` frame arrives;
 * read by `ModelsPanel` to render "last turn elapsed" under each
 * role row so a dev can see at a glance which role dragged the
 * just-finished turn (e.g. `nvidia/nemotron-3-super-120b-a12b:free`
 * regularly logs ~73s on primary, well above the policy bucket's
 * ~10-15s sum across constitution + repeat).
 *
 * Not persisted. The point is "what just happened on this dev
 * session"; the value resets to null on a hard reload, which is the
 * correct semantic since the model id pins also live in
 * localStorage and the dev may have switched models since.
 *
 * Why a store instead of prop-drilling from `agent-sheet`: ModelsPanel
 * is rendered alongside the SwitchPanel inside the sheet's builder
 * view but is otherwise a peer component; threading the agent stream
 * state through SwitchPanel just so ModelsPanel can read it would
 * couple two unrelated panels. The store decouples the read site
 * from the write site.
 */

export interface RoleTimings {
  /** Wall-time spent in the primary role's LLM (ms). */
  primaryMs: number;
  /** Sum of constitution + repeat detector wall-time (ms). */
  policyMs: number;
  /** Wall-time spent in the judge role (ms); 0 today since the
   *  judge isn't on the chat path. */
  judgeMs: number;
}

export interface RoleTimingsStore {
  /** `null` before the first turn completes on this session. */
  latest: RoleTimings | null;
  setLatest: (timings: RoleTimings) => void;
  reset: () => void;
}

export const useRoleTimings = create<RoleTimingsStore>()((set) => ({
  latest: null,
  setLatest: (timings) => set({ latest: timings }),
  reset: () => set({ latest: null }),
}));
