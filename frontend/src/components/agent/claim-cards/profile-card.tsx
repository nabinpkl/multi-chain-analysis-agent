"use client";

import type { Claim } from "@/lib/generated/Claim";
import { ProvenanceChip } from "../provenance/provenance-chip";

/**
 * Renders a `ClaimKind = Profile` card. Splits `body_markdown` on the
 * `${ref:N}` placeholders and inlines the corresponding provenance
 * chip in place. Chips picked by the render-surface derivation rule
 * (live focus on click for in-window wallets, modal trigger for
 * out-of-window).
 */
export function ProfileCard({
  claim,
  onModalRequest,
}: {
  claim: Claim;
  onModalRequest: () => void;
}) {
  const isRetracted = claim.policy_verdict.verdict === "retracted";
  return (
    <div
      className={
        "border border-mca-border rounded-md p-3 bg-mca-surface/40 space-y-2 " +
        (isRetracted ? "opacity-60" : "")
      }
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-muted">
          profile
        </span>
        <span className="text-[0.6rem] tabular-nums text-mca-dim">
          {claim.emitted_at_ms}ms
        </span>
      </div>
      <h3 className="text-sm text-mca-text leading-snug font-medium">
        {claim.headline}
      </h3>
      <p className="text-sm text-mca-text leading-relaxed">
        {renderBody(claim, onModalRequest)}
      </p>
      {claim.stubs_active.length > 0 ? (
        <p className="text-[0.6rem] uppercase tracking-[1.5px] text-amber-500/80 pt-1 border-t border-mca-border">
          via stubs: {claim.stubs_active.map((s) => shortName(s.name)).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function renderBody(claim: Claim, onModalRequest: () => void): React.ReactNode {
  // Split on the literal `${ref:N}` token. Reassemble with chips
  // interleaved.
  const parts: Array<React.ReactNode> = [];
  const re = /\$\{ref:(\d+)\}/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(claim.body_markdown)) !== null) {
    const before = claim.body_markdown.slice(lastIdx, match.index);
    if (before.length > 0) parts.push(<span key={`t-${key++}`}>{before}</span>);
    const refIdx = parseInt(match[1], 10);
    const ref = claim.provenance[refIdx];
    if (ref) {
      parts.push(
        <ProvenanceChip
          key={`r-${key++}`}
          refValue={ref}
          index={refIdx}
          onModalRequest={onModalRequest}
        />,
      );
    } else {
      parts.push(<span key={`m-${key++}`}>[ref:{refIdx}]</span>);
    }
    lastIdx = re.lastIndex;
  }
  const tail = claim.body_markdown.slice(lastIdx);
  if (tail.length > 0) parts.push(<span key={`t-${key++}`}>{tail}</span>);
  return parts;
}

function shortName(name: string): string {
  // policy.always_approve -> policy
  return name.split(".")[0];
}
