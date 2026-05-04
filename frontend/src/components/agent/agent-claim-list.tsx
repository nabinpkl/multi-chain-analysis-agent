"use client";

import { useState } from "react";
import {
  ClaimKind,
  type Claim,
} from "@/lib/wire/multichain/wire/agent/v1/claim_pb";
import type { AgentStatus, ChatTurn } from "@/hooks/use-agent-stream";
import type { ProgressEvent } from "./progress-format";
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
import { GatePathTimeline } from "./gate-path-timeline";
import { DiffBubble } from "./diff-bubble";

/**
 * Conversation-shaped renderer. Each ChatTurn renders as:
 *   - user message card (right-aligned, "you" tag)
 *   - optional Claim card (structured, with provenance chips)
 *   - optional Narrative bubble (free-form interpretation prose)
 *   - live progress placeholder while the turn is in flight
 *
 * Ship 1.6 split agent output into two channels (Claim + Narrative);
 * a turn can carry one, both, or neither. The user's message is shown
 * immediately on send so they have a record of what they asked while
 * the agent works. The pending placeholder reads the latest SSE
 * Progress phase so the user sees what stage the agent is at, not a
 * static "thinking..." that never updates.
 */
export function AgentClaimList({
  turns,
  status,
  progress,
  builderView,
}: {
  turns: ChatTurn[];
  status: AgentStatus;
  progress: ProgressEvent | null;
  builderView: boolean;
}) {
  const [modalSlice, setModalSlice] = useState<Claim | null>(null);

  return (
    <>
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {turns.map((turn, turnIdx) => {
          const pending =
            turn.claim === null &&
            turn.narrative === null &&
            turn.error === null &&
            turn.diffReply === null;
          const anchorId = `turn-anchor-${turnIdx}`;
          return (
            <div key={turn.id} id={anchorId} className="space-y-2 scroll-mt-4">
              <UserMessageCard
                text={turn.userText}
                pending={pending}
                progress={progress}
                builderView={builderView}
                errorMessage={turn.error}
                errorDebug={turn.errorDebug}
              />
              {turn.diffReply ? (
                <DiffBubble
                  diffReply={turn.diffReply}
                  onScrollToTurn={(t) => scrollToTurn(t)}
                />
              ) : (
                <>
                  {turn.claim ? (
                    <ClaimRender
                      claim={turn.claim}
                      onModalRequest={() => setModalSlice(turn.claim)}
                    />
                  ) : null}
                  {turn.narrative ? (
                    <NarrativeBubble
                      text={turn.narrative}
                      provenance={turn.narrativeProvenance}
                      retractedReason={turn.narrativeRetractedReason}
                      retractedDebug={turn.narrativeRetractedDebug}
                    />
                  ) : null}
                </>
              )}
              {builderView && turn.gatePaths.length > 0 ? (
                <div className="space-y-1.5 pt-1">
                  {turn.gatePaths.map((path, i) => (
                    <GatePathTimeline key={`${turn.id}-path-${i}`} path={path} />
                  ))}
                </div>
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
        slice={modalSlice?.subgraphSlice ?? null}

      />
    </>
  );
}

function scrollToTurn(turn: number): void {
  const el = document.getElementById(`turn-anchor-${turn}`);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function ClaimRender({
  claim,
  onModalRequest,
}: {
  claim: Claim;
  onModalRequest: () => void;
}) {
  if (claim.policyVerdict?.verdict.case === "retracted") {
    return <RetractedCard claim={claim} />;
  }
  const props = { claim, onModalRequest };
  // protoc-gen-es strips the `CLAIM_KIND_` prefix on the enum values
  // (idiomatic TS naming); on the wire the proto canonical JSON uses
  // the full names like "CLAIM_KIND_PROFILE", which fromJsonString
  // converts back to the numeric enum value here.
  switch (claim.kind) {
    case ClaimKind.PROFILE:
      return <ProfileCard {...props} />;
    case ClaimKind.PATTERN:
      return <PatternCard {...props} />;
    case ClaimKind.COMPARISON:
      return <ComparisonCard {...props} />;
    case ClaimKind.SUMMARY:
      return <SummaryCard {...props} />;
    case ClaimKind.PULSE:
      return <PulseCard {...props} />;
    default:
      return null;
  }
}
