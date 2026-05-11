"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { create } from "@bufbuild/protobuf";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useGraphFocus } from "@/stores/use-graph-focus";
import { useAgentSwitches } from "@/stores/use-agent-switches";
import { useLlmOverride } from "@/stores/use-llm-override";
import {
  AgentRequestSchema,
  type AgentRequest,
} from "@/lib/wire/multichain/wire/agent/v1/session_pb";
import {
  EntityRefSchema,
  EntityRefWalletSchema,
  ViewContextSchema,
} from "@/lib/wire/multichain/wire/agent/v1/entity_pb";
import {
  LlmOverrideSchema,
  RoleOverrideSchema,
} from "@/lib/wire/multichain/wire/agent/v1/llm_pb";
import type { AgentStatus } from "@/hooks/use-agent-stream";

/**
 * Input form for the agent. Reads focus + selection from
 * `useGraphFocus` on submit, builds an `AgentRequest` proto, hands
 * off to the parent's `onSend`. Disabled while a question is in
 * flight.
 */
export function AgentInput({
  onSend,
  onStop,
  status,
  liveWindowSecs,
}: {
  onSend: (request: AgentRequest) => void;
  /** Chunk 3.5: invoked when the user clicks the Stop button.
   *  Fires `DELETE /agent/turn/{thread_id}` then aborts the local
   *  fetch. Always required even though the button only shows
   *  while a turn is in flight, so the prop contract stays
   *  truthful about what the parent must wire. */
  onStop: () => void;
  status: AgentStatus;
  liveWindowSecs: number;
}) {
  const [text, setText] = useState("");
  const focusedAddr = useGraphFocus((s) => s.focusedAddr);
  const selection = useGraphFocus((s) => s.selection);
  const switches = useAgentSwitches((s) => s.switches);
  const builderViewOn = useAgentSwitches((s) => s.builderViewOn);
  const primaryOverride = useLlmOverride((s) => s.primary);
  const policyOverride = useLlmOverride((s) => s.policy);
  const judgeOverride = useLlmOverride((s) => s.judge);

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
    // Per-role LLM provider override (dev-only). Only attach the
    // wire field when at least one role is actually pinned;
    // production traffic carries an empty `LlmOverride` and the
    // backend defaults to env-driven OpenRouter for every role.
    const anyOverrideActive =
      primaryOverride.provider !== "" ||
      policyOverride.provider !== "" ||
      judgeOverride.provider !== "";
    const llmOverride = anyOverrideActive
      ? create(LlmOverrideSchema, {
          primary: create(RoleOverrideSchema, {
            provider: primaryOverride.provider,
            modelId: primaryOverride.modelId,
          }),
          policy: create(RoleOverrideSchema, {
            provider: policyOverride.provider,
            modelId: policyOverride.modelId,
          }),
          judge: create(RoleOverrideSchema, {
            provider: judgeOverride.provider,
            modelId: judgeOverride.modelId,
          }),
        })
      : undefined;

    const request = create(AgentRequestSchema, {
      userQuestion: trimmed,
      context: create(ViewContextSchema, {
        liveWindowSecs,
        focus: focusRef,
        selection: selectionRefs,
      }),
      switches,
      showTrace: builderViewOn,
      llmOverride,
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
        {inFlight ? (
          // Chunk 3.5: in-flight UI swaps Send for Stop so the user
          // can cancel a long codex turn. type="button" so Enter on
          // the textarea doesn't accidentally trigger Stop instead
          // of Send. The click fires `DELETE /agent/turn/{thread_id}`
          // server-side and aborts the local SSE fetch.
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={onStop}
            title="cancel the current turn"
          >
            stop
          </Button>
        ) : (
          <Button type="submit" size="sm" disabled={!text.trim()}>
            send
          </Button>
        )}
      </div>
    </form>
  );
}
