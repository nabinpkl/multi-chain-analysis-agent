"use client";

import type React from "react";
import type { ProvenanceRef } from "@/lib/wire/multichain/wire/shared/v1/provenance_pb";
import { ProvenanceChip } from "../provenance/provenance-chip";

/**
 * Shared `${ref:N}` renderer used by Claim cards (profile-card.tsx)
 * AND ship 5a's narrative bubble. Splits `text` on the `${ref:N}`
 * placeholder grammar; reassembles as alternating <span> and
 * <ProvenanceChip> nodes. Chips that can't resolve (out-of-bounds N
 * or missing entry) render as `[ref:N]` literal text so a malformed
 * model output degrades gracefully instead of crashing render.
 *
 * The regex `/\$\{ref:(\d+)\}/g` is the only regex on the frontend
 * after ship 5a. It targets a deterministic ASCII grammar the model
 * is instructed to emit; it doesn't try to interpret natural-
 * language meaning. ASCII-only, char-boundary-safe, no unicode
 * hazard.
 */
export function renderTextWithRefs(
  text: string,
  provenance: ProvenanceRef[],
  onModalRequest?: () => void,
): React.ReactNode {
  const parts: Array<React.ReactNode> = [];
  const re = /\$\{ref:(\d+)\}/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    const before = text.slice(lastIdx, match.index);
    if (before.length > 0) parts.push(<span key={`t-${key++}`}>{before}</span>);
    const refIdx = parseInt(match[1], 10);
    const ref = provenance[refIdx];
    if (ref) {
      parts.push(
        <ProvenanceChip
          key={`r-${key++}`}
          refValue={ref}
          index={refIdx}
          onModalRequest={onModalRequest ?? noop}
        />,
      );
    } else {
      parts.push(<span key={`m-${key++}`}>[ref:{refIdx}]</span>);
    }
    lastIdx = re.lastIndex;
  }
  const tail = text.slice(lastIdx);
  if (tail.length > 0) parts.push(<span key={`t-${key++}`}>{tail}</span>);
  return parts;
}

function noop() {
  // Default modal-request handler when caller doesn't need modal
  // routing (e.g. narrative bubble; out-of-window wallets there
  // could open the same subgraph modal in a future ship, but for
  // now the chip's click is an inert no-op rather than throwing).
}
