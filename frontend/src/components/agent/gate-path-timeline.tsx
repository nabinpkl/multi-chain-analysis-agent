"use client";

import type { GatePath } from "@/lib/wire/multichain/wire/agent/v1/sse_pb";
import type {
  PathState,
  PathStep,
  PolicyVerdict,
} from "@/lib/wire/multichain/wire/agent/v1/policy_pb";

/**
 * Ship 3.5 builder-view trace timeline. Renders a single
 * `GatePath` as a vertical list of steps with a state badge,
 * stage name (the switch label), and a single-line note. Cross-
 * check sub-modes are visually grouped under a "cross check"
 * header so the chain of consistency reads as one block.
 *
 * Color convention: green = approved, red = retracted, grey =
 * skipped / not-applicable.
 */
export function GatePathTimeline({ path }: { path: GatePath }) {
  return (
    <div className="border border-mca-border rounded bg-mca-surface px-3 py-2 space-y-1.5 text-[0.65rem]">
      <div className="flex items-center justify-between text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
        <span>{path.channel} gate</span>
        <FinalBadge verdict={path.finalVerdict} />
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
  const indent =
    step.stage.startsWith("narrative.cross_check.") ||
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

function StateBadge({ state }: { state: PathState | undefined }) {
  const inner = state?.state;
  if (inner?.case === "approved") {
    return (
      <span
        title="approved"
        className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full bg-green-500"
      />
    );
  }
  if (inner?.case === "retracted") {
    return (
      <span
        title={inner.value.reason}
        className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full bg-red-500"
      />
    );
  }
  // notApplicable or unset
  const detail = inner?.case === "notApplicable" ? inner.value.detail : undefined;
  return (
    <span
      title={detail}
      className="shrink-0 mt-0.5 inline-block w-2.5 h-2.5 rounded-full border border-mca-border bg-mca-bg"
    />
  );
}

function FinalBadge({ verdict }: { verdict: PolicyVerdict | undefined }) {
  // PolicyVerdict.verdict is the proto oneof.
  const inner = verdict?.verdict;
  if (inner?.case === "approved") {
    return (
      <span className="text-green-500 text-[0.55rem]">final approved</span>
    );
  }
  if (inner?.case === "retracted") {
    return (
      <span className="text-red-500 text-[0.55rem]" title={inner.value.reason}>
        final retracted
      </span>
    );
  }
  return null;
}
