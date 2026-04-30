"use client";

import { useState } from "react";
import type { Claim } from "@/lib/generated/Claim";
import type { AgentStatus, ChatTurn } from "@/hooks/use-agent-stream";
import { ProfileCard } from "./claim-cards/profile-card";
import {
  ComparisonCard,
  PatternCard,
  PulseCard,
  RetractedCard,
  SummaryCard,
} from "./claim-cards/other-cards";
import { UserMessageCard } from "./claim-cards/user-message-card";
import { NarrativeBubble } from "./claim-cards/narrative-bubble";
import { SubgraphModal } from "./provenance/subgraph-modal";

/**
 * Conversation-shaped renderer. Each ChatTurn renders as:
 *   - user message card (right-aligned, "you" tag)
 *   - optional Claim card (structured, with provenance chips)
 *   - optional Narrative bubble (free-form interpretation prose)
 *   - "thinking..." placeholder while nothing has arrived yet
 *
 * Ship 1.6 split agent output into two channels (Claim + Narrative);
 * a turn can carry one, both, or neither. The user's message is shown
 * immediately on send so they have a record of what they asked while
 * the agent works.
 */
export function AgentClaimList({
  turns,
  status,
}: {
  turns: ChatTurn[];
  status: AgentStatus;
}) {
  const [modalSlice, setModalSlice] = useState<Claim | null>(null);

  return (
    <>
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {turns.map((turn) => {
          const pending =
            turn.claim === null && turn.narrative === null && turn.error === null;
          return (
            <div key={turn.id} className="space-y-2">
              <UserMessageCard
                text={turn.userText}
                pending={pending}
                errorMessage={turn.error}
                errorDebug={turn.errorDebug}
              />
              {turn.claim ? (
                <ClaimRender
                  claim={turn.claim}
                  onModalRequest={() => setModalSlice(turn.claim)}
                />
              ) : null}
              {turn.narrative ? (
                <NarrativeBubble
                  text={turn.narrative}
                  retractedReason={turn.narrativeRetractedReason}
                  retractedDebug={turn.narrativeRetractedDebug}
                />
              ) : null}
            </div>
          );
        })}
        {status.kind === "error" && turns.length === 0 ? (
          <div className="text-xs text-amber-500 border border-mca-border rounded p-3">
            {status.message}
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

function ClaimRender({
  claim,
  onModalRequest,
}: {
  claim: Claim;
  onModalRequest: () => void;
}) {
  if (claim.policy_verdict.verdict === "retracted") {
    return <RetractedCard claim={claim} />;
  }
  const props = { claim, onModalRequest };
  switch (claim.kind) {
    case "profile":
      return <ProfileCard {...props} />;
    case "pattern":
      return <PatternCard {...props} />;
    case "comparison":
      return <ComparisonCard {...props} />;
    case "summary":
      return <SummaryCard {...props} />;
    case "pulse":
      return <PulseCard {...props} />;
    default:
      return null;
  }
}
