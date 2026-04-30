"use client";

import { useGraphFocus } from "@/stores/use-graph-focus";
import { useAgentStream } from "@/hooks/use-agent-stream";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { AgentEmptyState } from "./agent-empty-state";
import { AgentClaimList } from "./agent-claim-list";
import { AgentInput } from "./agent-input";
import { ProgressStrip } from "./progress-strip";
import { StubBanner } from "./stub-banner";

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
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  liveWindowSecs: number;
}) {
  const focusedAddr = useGraphFocus((s) => s.focusedAddr);
  const { status, claims, progress, ask, reset } = useAgentStream();
  const inFlight = status.kind === "sending" || status.kind === "streaming";

  return (
    <Sheet open={open} onOpenChange={onOpenChange} modal={false}>
      <SheetContent
        side="right"
        className="!max-w-[480px] w-full sm:!max-w-[480px] flex flex-col p-0 gap-0 bg-mca-bg"
      >
        <header className="px-4 py-3 border-b border-mca-border space-y-1">
          <div className="flex items-center justify-between gap-2 pr-8">
            <span className="text-[0.7rem] uppercase tracking-[2px] text-mca-text">
              agent
            </span>
            <button
              onClick={reset}
              disabled={status.kind === "idle" || inFlight}
              className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-muted hover:text-mca-text transition-colors px-2 py-1 border border-mca-border rounded disabled:opacity-30 disabled:hover:text-mca-muted"
            >
              new
            </button>
          </div>
          <FocusHeader focusedAddr={focusedAddr} />
        </header>

        <StubBanner enabled={open} />

        <ProgressStrip current={progress} active={inFlight} />

        {claims.length === 0 && !inFlight ? (
          <AgentEmptyState focusedAddr={focusedAddr} />
        ) : (
          <AgentClaimList claims={claims} status={status} />
        )}

        <AgentInput
          onSend={ask}
          status={status}
          liveWindowSecs={liveWindowSecs}
        />
      </SheetContent>
    </Sheet>
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
