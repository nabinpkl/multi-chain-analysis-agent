import { create } from "zustand";

/**
 * Source of Louvain community labels.
 *
 * - `backend`: backend's per-window analytics task computes Louvain off
 *   the main thread; the frontend consumes `AnalyticsBatch` SSE events
 *   and writes labels straight into `nodeToCommunityRef`. Default,
 *   keeps the main thread free at 50k+ edges.
 * - `frontend`: graphology-communities-louvain runs in the detect tick.
 *   Original behavior. Hits a main-thread freeze around 50k edges; kept
 *   as an opt-in for A/B comparison.
 *
 * Choice persists across reloads via localStorage so the user can A/B
 * compare during a demo without losing the setting on refresh. First
 * load with no stored value defaults to `backend`.
 */
export type LouvainSource = "frontend" | "backend";

const STORAGE_KEY = "mca:louvainSource";

function readInitial(): LouvainSource {
  if (typeof window === "undefined") return "backend";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "frontend" ? "frontend" : "backend";
}

interface AnalyticsState {
  louvainSource: LouvainSource;
  setLouvainSource: (s: LouvainSource) => void;
}

export const useAnalyticsStore = create<AnalyticsState>((set) => ({
  louvainSource: readInitial(),
  setLouvainSource: (s) => {
    set({ louvainSource: s });
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, s);
    }
  },
}));
