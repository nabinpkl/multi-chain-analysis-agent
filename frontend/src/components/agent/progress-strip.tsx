"use client";

import { Loader2Icon } from "lucide-react";

export interface ProgressEvent {
  phase: string;
  detail: string;
}

/**
 * Renders the current Progress event above the claim list. v0 surfaces
 * just the most recent phase + detail; ship 4+ may show a fuller
 * timeline. Only visible while the loop is in flight.
 *
 * Dual-mode text per audience:
 *
 * - Default user view (`builderView=false`): plain language a non-
 *   technical user can map to what they see in the UI. They know they
 *   asked about a wallet; "Agent is analyzing the wallet" tells them
 *   the model is working without leaking that there is a "primary
 *   model" or "constitution gate" or "primitive call" underneath.
 *   Same posture as AWS Bedrock chat or Anthropic Claude consumer
 *   surfaces: status text describes intent, not the API call shape.
 *
 * - Builder view (`builderView=true`): the same Progress phase carries
 *   the backend-specific detail string the loop driver emits
 *   ("primary model", "constitution gate", etc). Builder view is the
 *   audit/transparency surface for power users and dev iteration; the
 *   technical leak is the point there.
 */
export function ProgressStrip({
  current,
  active,
  builderView,
}: {
  current: ProgressEvent | null;
  active: boolean;
  builderView: boolean;
}) {
  if (!active && !current) return null;
  const text = current
    ? formatPhase(current.phase, current.detail, builderView)
    : builderView
      ? "preparing…"
      : "Agent is starting…";
  return (
    <div className="px-4 py-2 border-b border-mca-border bg-mca-bg flex items-center gap-2 text-xs text-mca-muted">
      {active ? (
        <Loader2Icon className="size-3 animate-spin text-mca-text" />
      ) : null}
      <span className="tabular-nums">{text}</span>
    </div>
  );
}

function formatPhase(phase: string, detail: string, builderView: boolean): string {
  if (builderView) {
    return formatBuilderPhase(phase, detail);
  }
  return formatUserPhase(phase);
}

/**
 * Plain-language status for the default user. Pure function of phase
 * (the detail string from the backend never reaches the user, by
 * design). Falls back to a generic "Agent is working…" so a phase the
 * frontend hasn't been taught about still looks intentional instead
 * of dumping `<phase>: <detail>` raw.
 */
function formatUserPhase(phase: string): string {
  switch (phase) {
    case "planning":
      return "Agent is planning…";
    case "tool_call":
      // The user knows what they focused on (wallet card, community
      // bubble); "looking up details" is more honest than naming a
      // primitive function they have no model for.
      return "Agent is looking up details…";
    case "drafting":
      return "Agent is analyzing…";
    case "judging":
      // Constitution gate framed as the model's own self-review. The
      // user doesn't need to know we run a separate policy LLM here.
      return "Agent is double-checking the answer…";
    case "synthesizing":
      return "Agent is finalizing…";
    default:
      return "Agent is working…";
  }
}

/**
 * Technical status for builder view. Carries the backend's detail
 * string verbatim so devs can map UI state to loop_driver call sites
 * and span names. The phases here are the ones loop_driver emits
 * today; new ones added there should grow this switch in the same PR.
 */
function formatBuilderPhase(phase: string, detail: string): string {
  switch (phase) {
    case "planning":
      return `planning… (${detail})`;
    case "tool_call":
      return `calling ${detail}…`;
    case "drafting":
      // Primary LLM call (~5s p50 on free-tier OpenRouter). Span:
      // agent.turn → agent run → chat <primary model>.
      return `drafting response… (${detail})`;
    case "judging":
      // Constitution gate (gpt-oss-20b policy LLM, issue #16, ~11s
      // p50). Span: gate.narrative_constitution → agent run → chat
      // <policy model>.
      return `judging response… (${detail})`;
    case "synthesizing":
      return `synthesizing (${detail})`;
    default:
      return `${phase}: ${detail}`;
  }
}
