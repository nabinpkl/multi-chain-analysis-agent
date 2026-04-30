"use client";

import { Loader2Icon } from "lucide-react";

/**
 * Renders the user's submitted question as a chat-style card. Shown
 * inline above the assistant's claim so the conversation reads as a
 * back-and-forth. While the claim is still pending, a "thinking..."
 * placeholder renders below this card.
 */
export function UserMessageCard({
  text,
  pending,
  errorMessage,
}: {
  text: string;
  pending: boolean;
  errorMessage: string | null;
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
          <span>thinking...</span>
        </div>
      ) : null}
      {errorMessage ? (
        <div className="text-xs text-amber-500 border border-amber-500/30 rounded p-2 bg-amber-500/5">
          {errorMessage}
        </div>
      ) : null}
    </div>
  );
}
