"use client";

import type { ProvenanceRef } from "@/lib/generated/ProvenanceRef";
import { useGraphFocus } from "@/stores/use-graph-focus";
import { cn } from "@/lib/utils";

/**
 * Renders a single provenance ref as a chip. Per the locked
 * render-surface derivation (plan + phase 03):
 *
 *   ref shape                                  -> surface
 *   ----------------------------------------------------------
 *   Wallet { idx: not null } in live snapshot  -> live focus
 *   Wallet { idx: null }                       -> modal (TODO ship 5)
 *   Edge in live snapshot                      -> live edge highlight (TODO)
 *   Community in live snapshot                 -> live community highlight (TODO)
 *   TimeRange                                  -> inline time chip
 *   Number                                     -> inline metric chip
 *
 * v0 implements live wallet focus on click; the others render as
 * inline non-interactive chips. Ship 5 wires the modal; ship 3 adds
 * edge / community highlight.
 */
export function ProvenanceChip({
  refValue,
  index,
  isFocused,
  onModalRequest,
}: {
  refValue: ProvenanceRef;
  index: number;
  isFocused?: boolean;
  onModalRequest?: () => void;
}) {
  const setFocus = useGraphFocus((s) => s.setFocus);

  switch (refValue.kind) {
    case "wallet": {
      // The primitive output knows `idx` from the snapshot; the model's
      // emit_claim payload does not (the model has no NodeIdx access).
      // v0 treats any non-empty addr as a live ref; if Sigma's graph
      // doesn't contain the node at click time, setFocus is a harmless
      // no-op. Ship 5 tightens this by populating `idx` server-side on
      // every Wallet provenance ref the runtime stamps.
      const isLiveRef = refValue.idx !== null || refValue.addr.length > 0;
      const handleClick = () => {
        if (isLiveRef) {
          setFocus(refValue.addr);
        } else {
          onModalRequest?.();
        }
      };
      return (
        <button
          type="button"
          onClick={handleClick}
          title={refValue.addr}
          className={cn(
            "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border align-baseline transition-colors",
            isLiveRef
              ? "border-emerald-500/40 text-mca-text hover:border-emerald-500"
              : "border-mca-border text-mca-muted hover:text-mca-text",
            isFocused && "bg-emerald-500/15",
          )}
        >
          <span className="opacity-50">w</span>
          {abbreviate(refValue.addr)}
        </button>
      );
    }
    case "edge":
      return (
        <span
          title={`edge ${refValue.id}`}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-muted align-baseline"
        >
          <span className="opacity-50">e</span>
          {refValue.id.slice(0, 8)}
        </span>
      );
    case "community":
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-text align-baseline">
          <span className="opacity-50">c</span>#{refValue.id}
        </span>
      );
    case "time-range":
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-muted align-baseline">
          <span className="opacity-50">t</span>
          {formatRange(refValue.from_s, refValue.to_s)}
        </span>
      );
    case "number":
      return (
        <span
          title={`${refValue.metric}: ${refValue.value}`}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-text align-baseline"
        >
          <span className="opacity-50">{refValue.metric}</span>
          {formatValue(refValue.value)}
        </span>
      );
    default:
      return null;
  }
  // index is unused in v0 but accepted so the renderer can dedupe later
  void index;
}

function abbreviate(addr: string): string {
  if (addr.length <= 10) return addr;
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`;
}

function formatValue(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  if (v === 0) return "0";
  return v.toPrecision(2);
}

function formatRange(fromS: number, toS: number): string {
  const fmt = (s: number) => {
    const d = new Date(s * 1000);
    return d.toLocaleTimeString();
  };
  return `${fmt(fromS)}…${fmt(toS)}`;
}
