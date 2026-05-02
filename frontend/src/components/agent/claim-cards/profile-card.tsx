"use client";

import type { Claim } from "@/lib/generated/Claim";
import { renderTextWithRefs } from "./render-with-refs";

/**
 * Renders a `ClaimKind = Profile` card. Splits `body_markdown` on the
 * `${ref:N}` placeholders and inlines the corresponding provenance
 * chip in place via the shared `renderTextWithRefs` helper (also
 * used by the narrative bubble in ship 5a).
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
        {renderTextWithRefs(claim.body_markdown, claim.provenance, onModalRequest)}
      </p>
      {claim.stubs_active.length > 0 ? (
        <p className="text-[0.6rem] uppercase tracking-[1.5px] text-amber-500/80 pt-1 border-t border-mca-border">
          via stubs: {claim.stubs_active.map((s) => shortName(s.name)).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function shortName(name: string): string {
  // policy.always_approve -> policy
  return name.split(".")[0];
}
