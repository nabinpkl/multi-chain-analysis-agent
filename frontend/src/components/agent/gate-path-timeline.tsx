"use client";

import type { GatePath } from "@/lib/generated/GatePath";
import type { PathState } from "@/lib/generated/PathState";
import type { PathStep } from "@/lib/generated/PathStep";

/**
 * Ship 3.5 builder-view trace timeline. Renders a single
 * `GatePath` as a vertical list of steps with a state badge,
 * stage name (the switch label), and a single-line note. Cross-
 * check sub-modes are visually grouped under a "cross check"
 * header so the chain of consistency reads as one block.
 *
 * Color convention shared with the rest of the agent panel:
 * green = approved, red = retracted, grey = skipped /
 * not-applicable.
 */
export function GatePathTimeline({ path }: { path: GatePath }) {
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-1.5 text-[0.65rem]">
      <div className="flex items-center justify-between text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
        <span>{path.channel} gate</span>
        <FinalBadge verdict={path.final_verdict} />
      </div>
      <ul className="space-y-1">
        {path.steps.map((step, i) => (
          <PathStepRow key={i} step={step} />
        ))}
      </ul>
    </div>
  );
}

function PathStepRow({ step }: { step: PathStep }) {
  const indent = step.stage.startsWith("narrative.cross_check.") ||
    step.stage.startsWith("claim.cross_check.");
  const label = stageLabel(step.stage);
  return (
    <li
      className={`flex items-start gap-2 leading-tight ${indent ? "pl-4" : ""}`}
    >
      <StateBadge state={step.state} />
      <div className="flex-1 min-w-0">
        <span className="block text-mca-text">{label}</span>
        <span className="block text-[0.6rem] text-mca-dim">{step.note}</span>
      </div>
    </li>
  );
}

function stageLabel(stage: string): string {
  // narrative.cross_check.text_match -> "text match"
  // narrative.stay_in_role -> "stay in role"
  const parts = stage.split(".");
  const last = parts[parts.length - 1];
  return last.replace(/_/g, " ");
}

function StateBadge({ state }: { state: PathState }) {
  if (state.state === "approved") {
    return (
      <span
        title="approved"
        className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full bg-green-500"
      />
    );
  }
  if (state.state === "retracted") {
    return (
      <span
        title={state.reason}
        className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full bg-red-500"
      />
    );
  }
  return (
    <span
      title={state.detail}
      className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full border border-mca-border bg-mca-bg"
    />
  );
}

function FinalBadge({
  verdict,
}: {
  verdict: GatePath["final_verdict"];
}) {
  // PolicyVerdict is { verdict: "approved" } | { verdict: "retracted",
  // reason: "..." } via the kebab-case rename_all serde tag.
  if (verdict.verdict === "approved") {
    return (
      <span className="text-green-500 text-[0.55rem]">final approved</span>
    );
  }
  return (
    <span className="text-red-500 text-[0.55rem]" title={verdict.reason}>
      final retracted
    </span>
  );
}
