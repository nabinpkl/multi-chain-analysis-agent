"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { create } from "@bufbuild/protobuf";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useGraphFocus } from "@/stores/use-graph-focus";
import { useAgentSwitches } from "@/stores/use-agent-switches";
import {
  AgentRequestSchema,
  type AgentRequest,
} from "@/lib/wire/multichain/wire/agent/v1/session_pb";
import {
  EntityRefSchema,
  EntityRefWalletSchema,
  ViewContextSchema,
} from "@/lib/wire/multichain/wire/agent/v1/entity_pb";
import type { AgentStatus } from "@/hooks/use-agent-stream";

/**
 * Input form for the agent. Reads focus + selection from
 * `useGraphFocus` on submit, builds an `AgentRequest` proto, hands
 * off to the parent's `onSend`. Disabled while a question is in
 * flight.
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
  const switches = useAgentSwitches((s) => s.switches);
  const builderViewOn = useAgentSwitches((s) => s.builderViewOn);

  const inFlight = status.kind === "sending" || status.kind === "streaming";

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || inFlight) return;
    // `threadId` is set by `useAgentStream` from the currently-tracked
    // thread; we leave it unset here.
    //
    // Ship 3.5: switches + showTrace come from the per-page zustand
    // store. `showTrace` mirrors the builder-view toggle so the backend
    // skips emitting GatePath frames for casual visitors.
    const focusRef = focusedAddr
      ? create(EntityRefSchema, {
          entity: {
            case: "wallet",
            value: create(EntityRefWalletSchema, { id: focusedAddr }),
          },
        })
      : undefined;
    const selectionRefs = selection.map((addr) =>
      create(EntityRefSchema, {
        entity: {
          case: "wallet",
          value: create(EntityRefWalletSchema, { id: addr }),
        },
      }),
    );
    const request = create(AgentRequestSchema, {
      userQuestion: trimmed,
      context: create(ViewContextSchema, {
        liveWindowSecs,
        focus: focusRef,
        selection: selectionRefs,
      }),
      switches,
      showTrace: builderViewOn,
    });
    onSend(request);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Standard chat UX: plain Enter sends, Shift+Enter inserts a
    // newline. Cmd/Ctrl+Enter also sends so existing users don't
    // get a regression. IME composition is excluded so non-Latin
    // input methods can use Enter to confirm composition without
    // sending.
    if (
      e.key === "Enter" &&
      !e.shiftKey &&
      !e.nativeEvent.isComposing
    ) {
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
          enter to send · shift+enter for newline
        </span>
        <Button type="submit" size="sm" disabled={inFlight || !text.trim()}>
          {inFlight ? "..." : "send"}
        </Button>
      </div>
    </form>
  );
}
