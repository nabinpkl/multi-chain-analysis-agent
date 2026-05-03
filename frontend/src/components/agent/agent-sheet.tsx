"use client";

import { useGraphFocus } from "@/stores/use-graph-focus";
import { useAgentSwitches } from "@/stores/use-agent-switches";
import type { AgentStreamState } from "@/hooks/use-agent-stream";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { AgentEmptyState } from "./agent-empty-state";
import { AgentClaimList } from "./agent-claim-list";
import { AgentInput } from "./agent-input";
import { BuilderViewToggle } from "./builder-view-toggle";
import { ProgressStrip } from "./progress-strip";
import { SwitchPanel } from "./switch-panel";

/**
 * Right-side overlay panel for the agent. Per D-3 + D-5 in
 * `architecture-decisions/chain-analysis-agent/01-agent-overview.md`,
 * the sheet floats over the graph (modal=false; canvas stays
 * visible+interactive behind it) so provenance chips can highlight
 * nodes on the live canvas while the user reads the response.
 *
 * Mounts (top-down): stub banner (always visible while open),
 * progress strip (visible during loop), claim list (per-ClaimKind
 * dispatcher) OR empty state, input box.
 */
export function AgentSheet({
  open,
  onOpenChange,
  liveWindowSecs,
  agentStream,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  liveWindowSecs: number;
  agentStream: AgentStreamState;
}) {
  const focusedAddr = useGraphFocus((s) => s.focusedAddr);
  const builderViewOn = useAgentSwitches((s) => s.builderViewOn);
  const { status, turns, progress, threadId, turn, ask, reset } = agentStream;
  const inFlight = status.kind === "sending" || status.kind === "streaming";
  const showTurnChip = threadId !== null && (turn > 0 || turns.length > 0);

  return (
    <Sheet open={open} onOpenChange={onOpenChange} modal={false}>
      <SheetContent
        side="right"
        className="!max-w-[480px] w-full sm:!max-w-[480px] flex flex-col p-0 gap-0 bg-mca-bg"
      >
        <header className="px-4 py-3 border-b border-mca-border space-y-1">
          <div className="flex items-center justify-between gap-2 pr-8">
            <span className="text-[0.7rem] uppercase tracking-[2px] text-mca-text flex items-center gap-2">
              agent
              {showTurnChip ? (
                <span className="text-[0.55rem] tabular-nums text-mca-muted normal-case border border-mca-border rounded px-1.5 py-0.5">
                  turn {turn + 1}
                </span>
              ) : null}
            </span>
            <div className="flex items-center gap-3">
              <BuilderViewToggle />
              <button
                onClick={reset}
                disabled={status.kind === "idle" && turns.length === 0}
                className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-muted hover:text-mca-text transition-colors px-2 py-1 border border-mca-border rounded disabled:opacity-30 disabled:hover:text-mca-muted"
                title="start a new thread"
              >
                new
              </button>
            </div>
          </div>
          <FocusHeader focusedAddr={focusedAddr} />
        </header>

        {builderViewOn ? <SwitchPanel /> : null}

        <ProgressStrip current={progress} active={inFlight} />

        {turns.length === 0 && !inFlight ? (
          <AgentEmptyState focusedAddr={focusedAddr} />
        ) : (
          <AgentClaimList turns={turns} status={status} />
        )}

        <DisclaimerFooter />

        <AgentInput
          onSend={ask}
          status={status}
          liveWindowSecs={liveWindowSecs}
        />
      </SheetContent>
    </Sheet>
  );
}

/**
 * Permanent, non-dismissable disclaimer above the input. Ship 1.6
 * paired this with the new Narrative output channel: now that the
 * model can speak in free-form prose, the user has to know that
 * interpretive statements are not yet cross-checked against the
 * underlying data. The `narrative.no_factuality_gate` stub names this
 * gap in the diagnostics banner; this footer is the user-facing half
 * of the same warning. Visible = remembered, per the ship-1
 * stub-visibility philosophy.
 */
function DisclaimerFooter() {
  return (
    <div className="px-4 py-2 border-t border-mca-border bg-mca-bg/60">
      <p className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-dim leading-relaxed">
        numbers + provenance come from live on-chain data. interpretations are
        model-generated and can be wrong even when numbers are right. click any
        chip to verify the source.
      </p>
    </div>
  );
}

function FocusHeader({ focusedAddr }: { focusedAddr: string | null }) {
  if (!focusedAddr) {
    return (
      <p className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-dim">
        no focus  click a wallet
      </p>
    );
  }
  const abbr =
    focusedAddr.length > 10
      ? `${focusedAddr.slice(0, 4)}...${focusedAddr.slice(-4)}`
      : focusedAddr;
  return (
    <p className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-muted">
      focused: <span className="text-mca-text tabular-nums normal-case">{abbr}</span>
    </p>
  );
}
