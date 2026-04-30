"use client";

import type { Claim } from "@/lib/generated/Claim";
import { ProfileCard } from "./profile-card";

/**
 * Skeletons for the other `ClaimKind` variants. Each delegates to the
 * Profile renderer in v0 (same shape: headline + body with provenance
 * chips). Ship 3 fills `pattern-card.tsx` and `summary-card.tsx`;
 * ship 5 fills `comparison-card.tsx`; ship 7 fills `pulse-card.tsx`
 * with hedge-coded styling. The dispatcher in `agent-claim-list.tsx`
 * routes by `claim.kind` so adding the real renderer is a single-file
 * change.
 */

export function PatternCard(props: { claim: Claim; onModalRequest: () => void }) {
  return <ProfileCard {...props} />;
}

export function ComparisonCard(props: { claim: Claim; onModalRequest: () => void }) {
  return <ProfileCard {...props} />;
}

export function SummaryCard(props: { claim: Claim; onModalRequest: () => void }) {
  return <ProfileCard {...props} />;
}

export function PulseCard(props: { claim: Claim; onModalRequest: () => void }) {
  return <ProfileCard {...props} />;
}

/**
 * Renderer for claims with `policy_verdict = Retracted`. Ship 2
 * starts producing these. The styling is intentionally distinct
 * (greyed, struck-through headline) so the user sees the retraction
 * while still having the chance to read why.
 */
export function RetractedCard({ claim }: { claim: Claim }) {
  if (claim.policy_verdict.verdict !== "retracted") return null;
  return (
    <div className="border border-amber-500/40 rounded-md p-3 bg-amber-500/5 space-y-2 opacity-80">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[0.6rem] uppercase tracking-[1.5px] text-amber-500">
          retracted
        </span>
        <span className="text-[0.6rem] tabular-nums text-mca-dim">
          {claim.emitted_at_ms}ms
        </span>
      </div>
      <h3 className="text-sm text-mca-muted line-through">{claim.headline}</h3>
      <p className="text-xs text-mca-muted leading-relaxed">
        Output policy retracted this claim:{" "}
        <span className="text-mca-text">{claim.policy_verdict.reason}</span>
      </p>
    </div>
  );
}
