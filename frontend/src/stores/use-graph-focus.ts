import { create } from "zustand";

/**
 * Focus and selection state for the live graph canvas. Sigma's
 * `clickNode` / `enterNode` / `leaveNode` events write here; the
 * agent panel reads to populate `ViewContext.focus` on send.
 *
 * Per D-6 (architecture-decisions/chain-analysis-agent/01-agent-overview.md):
 * structured frontend context is the strongest disambiguation signal
 * for the agent. "This wallet" / "these wallets" resolve against the
 * fields below, not against model heuristic on the raw question.
 *
 * No persistence: focus is ephemeral, lives only while the user is
 * looking at the graph. Refresh clears.
 */
interface GraphFocusState {
  /** Last clicked wallet pubkey (sticky until cleared or re-clicked). */
  focusedAddr: string | null;
  /** Currently hovered wallet pubkey (transient; null off-node). */
  hoveredAddr: string | null;
  /** Multi-select. Empty in v0; the field is here so the wire seam
   * with `ViewContext.selection` is settled. */
  selection: string[];
  setFocus: (addr: string | null) => void;
  setHover: (addr: string | null) => void;
  toggleSelection: (addr: string) => void;
  clearSelection: () => void;
}

export const useGraphFocus = create<GraphFocusState>((set) => ({
  focusedAddr: null,
  hoveredAddr: null,
  selection: [],
  setFocus: (focusedAddr) => set({ focusedAddr }),
  setHover: (hoveredAddr) => set({ hoveredAddr }),
  toggleSelection: (addr) =>
    set((s) => {
      const idx = s.selection.indexOf(addr);
      if (idx >= 0) {
        const next = s.selection.slice();
        next.splice(idx, 1);
        return { selection: next };
      }
      return { selection: [...s.selection, addr] };
    }),
  clearSelection: () => set({ selection: [] }),
}));
