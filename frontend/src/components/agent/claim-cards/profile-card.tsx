"use client";

import type { Claim } from "@/lib/wire/multichain/wire/agent/v1/claim_pb";
import { renderTextWithRefs } from "./render-with-refs";

/**
 * Renders a `ClaimKind = Profile` card. Splits `bodyMarkdown` on the
 * `${ref:N}` placeholders and inlines the corresponding provenance
 * chip in place via the shared `renderTextWithRefs` helper.
 */
export function ProfileCard({
  claim,
  onModalRequest,
}: {
  claim: Claim;
  onModalRequest: () => void;
}) {
  const isRetracted = claim.policyVerdict?.verdict.case === "retracted";
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
          {claim.emittedAtMs}ms
        </span>
      </div>
      <h3 className="text-sm text-mca-text leading-snug font-medium">
        {claim.headline}
      </h3>
      <p className="text-sm text-mca-text leading-relaxed">
        {renderTextWithRefs(claim.bodyMarkdown, claim.provenance, onModalRequest)}
      </p>
      {claim.stubsActive.length > 0 ? (
        <p className="text-[0.6rem] uppercase tracking-[1.5px] text-amber-500/80 pt-1 border-t border-mca-border">
          via stubs: {claim.stubsActive.map((s) => shortName(s.name)).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function shortName(name: string): string {
  // policy.always_approve -> policy
  return name.split(".")[0];
}
