"use client";

import { useState } from "react";
import type { Claim } from "@/lib/generated/Claim";
import type { AgentStatus } from "@/hooks/use-agent-stream";
import { ProfileCard } from "./claim-cards/profile-card";
import {
  ComparisonCard,
  PatternCard,
  PulseCard,
  RetractedCard,
  SummaryCard,
} from "./claim-cards/other-cards";
import { SubgraphModal } from "./provenance/subgraph-modal";

/**
 * Per-ClaimKind dispatcher. v0 only emits Profile; the other cards
 * render via Profile as a placeholder until ship 3/5/7 fill them.
 * Retracted claims (ship 2) get the dedicated RetractedCard.
 */
export function AgentClaimList({
  claims,
  status,
}: {
  claims: Claim[];
  status: AgentStatus;
}) {
  const [modalSlice, setModalSlice] = useState<Claim | null>(null);

  return (
    <>
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {claims.map((claim) => {
          if (claim.policy_verdict.verdict === "retracted") {
            return <RetractedCard key={claim.id} claim={claim} />;
          }
          const props = {
            claim,
            onModalRequest: () => setModalSlice(claim),
          };
          switch (claim.kind) {
            case "profile":
              return <ProfileCard key={claim.id} {...props} />;
            case "pattern":
              return <PatternCard key={claim.id} {...props} />;
            case "comparison":
              return <ComparisonCard key={claim.id} {...props} />;
            case "summary":
              return <SummaryCard key={claim.id} {...props} />;
            case "pulse":
              return <PulseCard key={claim.id} {...props} />;
            default:
              return null;
          }
        })}
        {status.kind === "error" ? (
          <div className="text-xs text-amber-500 border border-mca-border rounded p-3">
            {status.message}
          </div>
        ) : null}
        {status.kind === "done" && claims.length > 0 ? (
          <div className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-dim pt-1">
            done in {status.elapsedMs}ms
          </div>
        ) : null}
      </div>
      <SubgraphModal
        open={modalSlice !== null}
        onOpenChange={(open) => {
          if (!open) setModalSlice(null);
        }}
        slice={modalSlice?.subgraph_slice ?? null}
      />
    </>
  );
}
