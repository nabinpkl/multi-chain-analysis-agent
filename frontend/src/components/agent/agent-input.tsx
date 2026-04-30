"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useGraphFocus } from "@/stores/use-graph-focus";
import type { AgentRequest } from "@/lib/generated/AgentRequest";
import type { AgentStatus } from "@/hooks/use-agent-stream";

/**
 * Input form for the agent. Reads focus + selection from
 * `useGraphFocus` on submit, builds an `AgentRequest`, hands off to
 * the parent's `onSend`. Disabled while a question is in flight.
 */
export function AgentInput({
  onSend,
  status,
  liveWindowSecs,
}: {
  onSend: (request: AgentRequest) => void;
  status: AgentStatus;
  liveWindowSecs: number;
}) {
  const [text, setText] = useState("");
  const focusedAddr = useGraphFocus((s) => s.focusedAddr);
  const selection = useGraphFocus((s) => s.selection);

  const inFlight = status.kind === "sending" || status.kind === "streaming";

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || inFlight) return;
    const request: AgentRequest = {
      user_question: trimmed,
      context: {
        live_window_secs: liveWindowSecs,
        focus: focusedAddr ? { kind: "wallet", id: focusedAddr } : null,
        selection: selection.map((addr) => ({ kind: "wallet" as const, id: addr })),
      },
    };
    onSend(request);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Enter sends; bare Enter inserts a newline (so multi-line
    // questions are easy and stray Enter does not submit).
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      onSubmit={submit}
      className="border-t border-mca-border p-3 space-y-2 bg-mca-bg"
    >
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={
          focusedAddr
            ? "ask about the focused wallet, or anything in view..."
            : "ask a question. click a wallet to set focus first."
        }
        rows={3}
        disabled={inFlight}
        className="text-sm resize-none"
      />
      <div className="flex items-center justify-between gap-2">
        <span className="text-[0.6rem] uppercase tracking-[1.5px] text-mca-muted">
          ⌘+enter to send
        </span>
        <Button type="submit" size="sm" disabled={inFlight || !text.trim()}>
          {inFlight ? "..." : "send"}
        </Button>
      </div>
    </form>
  );
}
