/**
 * Phase formatter for the SSE Progress event.
 *
 * Was previously a `ProgressStrip` React component sitting at the top
 * of the agent sheet. The strip drifted from the per-turn pending
 * placeholder ("thinking...") and the user saw two indicators saying
 * different things at the same time. Merged into the per-turn
 * placeholder; this module is now formatter-only and the rendering
 * lives in `claim-cards/user-message-card.tsx`.
 *
 * Dual-mode by audience:
 *
 * - Default user view (`builderView=false`): plain language a non-
 *   technical user can map to what they see in the UI. Same posture
 *   as AWS Bedrock chat or Anthropic Claude consumer surfaces:
 *   describe intent, not the API call shape.
 *
 * - Builder view (`builderView=true`): same Progress phase carries
 *   the backend-specific detail string the loop driver emits
 *   ("primary model", "constitution gate", etc). Builder view is the
 *   audit / dev iteration surface; the technical leak is the point.
 */

export interface ProgressEvent {
  phase: string;
  detail: string;
}

export function formatProgressPhase(
  current: ProgressEvent | null,
  builderView: boolean,
): string {
  if (current === null) {
    return builderView ? "preparing…" : "Agent is starting…";
  }
  if (builderView) {
    return formatBuilderPhase(current.phase, current.detail);
  }
  return formatUserPhase(current.phase);
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
      // mcae.turn → agent run → chat <primary model>.
      return `drafting response… (${detail})`;
    case "judging":
      // Constitution gate (gpt-oss-20b policy LLM, issue #16, ~11s
      // p50). Span: mcae.gate.narrative_constitution → agent run →
      // chat <policy model>.
      return `judging response… (${detail})`;
    case "synthesizing":
      return `synthesizing (${detail})`;
    default:
      return `${phase}: ${detail}`;
  }
}
