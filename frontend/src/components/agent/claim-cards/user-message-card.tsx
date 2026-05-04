"use client";

import { Loader2Icon } from "lucide-react";

import {
  formatProgressPhase,
  type ProgressEvent,
} from "../progress-format";

/**
 * Renders the user's submitted question as a chat-style card. Shown
 * inline above the assistant's claim so the conversation reads as a
 * back-and-forth. While the claim is still pending, a live progress
 * placeholder renders below this card carrying the latest SSE
 * Progress phase (planning / drafting / judging / etc) formatted for
 * the active audience (default user vs builder view).
 *
 * Was previously a static "thinking..." string here AND a separate
 * sticky strip at the top of the sheet showing the same progress.
 * The two drifted (top said "Agent is double-checking the answer…",
 * bottom said "thinking..."). Merged: only this placeholder renders
 * the progress now.
 *
 * Ship 2.6.1: `errorDebug` is the raw underlying error (rig prompt
 * failure, HTTP status, etc.)  present only when the backend ships
 * with `AGENT_DEBUG_PUBLIC=1`. Rendered as a small monospace block
 * under the friendly `errorMessage` so the dev sees what really
 * went wrong without that detail leaking to prod users.
 */
export function UserMessageCard({
  text,
  pending,
  progress,
  builderView,
  errorMessage,
  errorDebug,
}: {
  text: string;
  pending: boolean;
  progress: ProgressEvent | null;
  builderView: boolean;
  errorMessage: string | null;
  errorDebug?: string | null;
}) {
  return (
    <div className="space-y-2">
      <div className="ml-auto max-w-[85%] border border-emerald-500/30 rounded-md p-3 bg-emerald-500/5 space-y-1">
        <div className="text-[0.6rem] uppercase tracking-[1.5px] text-emerald-500/80">
          you
        </div>
        <p className="text-sm text-mca-text leading-relaxed whitespace-pre-wrap">
          {text}
        </p>
      </div>
      {pending ? (
        <div className="flex items-center gap-2 text-xs text-mca-muted px-1">
          <Loader2Icon className="size-3 animate-spin" />
          <span className="tabular-nums">
            {formatProgressPhase(progress, builderView)}
          </span>
        </div>
      ) : null}
      {errorMessage ? (
        <div className="text-xs text-amber-500 border border-amber-500/30 rounded p-2 bg-amber-500/5 space-y-1">
          <p>{errorMessage}</p>
          {errorDebug ? (
            <pre className="text-[0.6rem] font-mono text-mca-dim leading-snug whitespace-pre-wrap break-all bg-amber-500/5 rounded px-2 py-1 border border-amber-500/20">
              <span className="text-amber-500/60">debug</span> {errorDebug}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
