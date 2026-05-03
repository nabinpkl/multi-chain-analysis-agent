"use client";

import type { ProvenanceRef } from "@/lib/wire/multichain/wire/shared/v1/provenance_pb";
import { useGraphFocus } from "@/stores/use-graph-focus";
import { cn } from "@/lib/utils";

/**
 * Renders a single provenance ref as a chip. Per the locked
 * render-surface derivation:
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
 * `refValue.ref` is the proto oneof: `{ case: "wallet", value: WalletRef }`
 * etc. Switch on `case`.
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
  const ref = refValue.ref;

  switch (ref.case) {
    case "wallet": {
      const wallet = ref.value;
      // Bufbuild leaves optional uint32 as undefined when not present.
      const isLiveRef = wallet.idx !== undefined || wallet.addr.length > 0;
      const handleClick = () => {
        if (isLiveRef) {
          setFocus(wallet.addr);
        } else {
          onModalRequest?.();
        }
      };
      return (
        <button
          type="button"
          onClick={handleClick}
          title={wallet.addr}
          className={cn(
            "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border align-baseline transition-colors",
            isLiveRef
              ? "border-emerald-500/40 text-mca-text hover:border-emerald-500"
              : "border-mca-border text-mca-muted hover:text-mca-text",
            isFocused && "bg-emerald-500/15",
          )}
        >
          <span className="opacity-50">w</span>
          {abbreviate(wallet.addr)}
        </button>
      );
    }
    case "edge": {
      const edge = ref.value;
      return (
        <span
          title={`edge ${edge.id}`}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-muted align-baseline"
        >
          <span className="opacity-50">e</span>
          {edge.id.slice(0, 8)}
        </span>
      );
    }
    case "community":
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-text align-baseline">
          <span className="opacity-50">c</span>#{ref.value.id}
        </span>
      );
    case "timeRange": {
      const tr = ref.value;
      return (
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-muted align-baseline">
          <span className="opacity-50">t</span>
          {formatRange(tr.fromS, tr.toS)}
        </span>
      );
    }
    case "number": {
      const n = ref.value;
      return (
        <span
          title={`${n.metric}: ${n.value}`}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[0.65rem] tabular-nums border border-mca-border text-mca-text align-baseline"
        >
          <span className="opacity-50">{n.metric}</span>
          {formatValue(n.value)}
        </span>
      );
    }
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
