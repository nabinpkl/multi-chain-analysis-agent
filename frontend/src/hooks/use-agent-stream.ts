"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { create, fromJsonString, toJsonString } from "@bufbuild/protobuf";

import {
  AgentDoneSchema,
  AgentRequestSchema,
  type AgentRequest,
} from "@/lib/wire/multichain/wire/agent/v1/session_pb";
import {
  ClaimSchema,
  type Claim,
} from "@/lib/wire/multichain/wire/agent/v1/claim_pb";
import {
  ChangedSinceSchema,
  NoMovementSchema,
  type ChangedSince,
  type NoMovement,
} from "@/lib/wire/multichain/wire/agent/v1/diff_pb";
import {
  GatePathSchema,
  type GatePath,
} from "@/lib/wire/multichain/wire/agent/v1/sse_pb";
import {
  NarrativeDeltaSchema,
  NarrativeRetractedSchema,
  NarrativeWithRefsSchema,
} from "@/lib/wire/multichain/wire/agent/v1/narrative_pb";
import { type ProvenanceRef } from "@/lib/wire/multichain/wire/shared/v1/provenance_pb";
import { useRoleTimings } from "@/stores/use-role-timings";
import { parseSseStream } from "@/lib/sse-parser";
import {
  clearThreadId,
  getThreadId,
  setThreadId as persistThreadId,
} from "@/lib/session";
import { useRuntimeSelector } from "@/stores/use-runtime-selector";

import type { ProgressEvent } from "@/components/agent/progress-format";

const DEFAULT_AGENT_URL = "http://localhost:8003";

/**
 * Python agent service hosts every `/agent/*` route. Per the AGENTS.md
 * "Wire format per hop" matrix the browser hop is proto canonical JSON
 * (camelCase, oneof case as the wrapping key). The Rust data plane on
 * `NEXT_PUBLIC_API_URL` still serves graph data and never talks to the
 * agent.
 */
function agentUrl(): string {
  return process.env.NEXT_PUBLIC_AGENT_URL || DEFAULT_AGENT_URL;
}

export type AgentStatus =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "streaming"; threadId: string }
  | {
      kind: "done";
      threadId: string;
      elapsedMs: number;
      /**
       * 32-hex-char OTel trace id stamped on the Done frame by the
       * loop driver (Ship 1 of agent-observability, ADR 13). The
       * agent sheet renders a deep-link to Langfuse for this trace.
       * Empty string when telemetry is disabled.
       */
      traceId: string;
      /**
       * Per-role wall-time tally for this turn (ms). Sum across
       * multiple calls within the same role (the policy bucket
       * fires multiple times per turn for constitution gates +
       * repeat detection, so the sum is the useful "how much wall
       * time did this role consume" view). The builder view's
       * Models panel reads this back to surface "primary 73.8s
       * last call" under each role row.
       */
      roleTimings: { primaryMs: number; policyMs: number; judgeMs: number };
    }
  | { kind: "error"; message: string };

/**
 * One round of conversation: the user's question + the resulting
 * agent output. Ship 1.6 split output into two channels: a structured
 * `claim` (card with chips) and a free-form `narrative` (interpretation
 * bubble). Ship 2 added the constitution gate: narrative the gate
 * retracts arrives with `narrativeRetractedReason` set, and the bubble
 * renders in struck-through amber styling instead of normal prose.
 *
 * Ship 2.6.1: `*Debug*` fields carry diagnostic detail when the
 * backend is started with `AGENT_DEBUG_PUBLIC=1`.
 *
 * A turn may carry any combination: claim only, narrative only, both,
 * a retracted narrative, or nothing (the Done-fallback flags the last
 * case as errored so the spinner doesn't hang).
 */
export interface ChatTurn {
  id: string;
  userText: string;
  sentAtMs: number;
  claim: Claim | null;
  narrative: string | null;
  /**
   * Ship 5a: typed citation array assembled by the backend from
   * this turn's emitted Claims (concatenated provenance arrays in
   * emission order). The narrative bubble passes this to
   * `renderTextWithRefs` to substitute `${ref:N}` chips inline.
   */
  narrativeProvenance: ProvenanceRef[];
  narrativeRetractedReason: string | null;
  narrativeRetractedDebug: string | null;
  error: string | null;
  errorDebug: string | null;
  /**
   * Ship 3.5 builder-view trace. Only populated when the request
   * was sent with `showTrace: true`.
   */
  gatePaths: GatePath[];
  /**
   * Ship 4 `dontRepeatYourself` payload. Mutually exclusive with
   * the normal claim/narrative path.
   */
  diffReply:
    | { kind: "no-movement"; payload: NoMovement }
    | { kind: "changed-since"; payload: ChangedSince }
    | null;
}

/**
 * Owns the agent thread across the browser's lifetime (not just the
 * tab session).
 *
 * `threadId` lives in `localStorage["mca:threadId"]` so a page refresh
 * resumes the same conversation; the server reads its persisted
 * `state.json` and the next turn carries prior `message_history`,
 * `claims`, and `bindings`. `reset()` clears the local thread_id
 * ("new chat" button). On a 404 from the server (stale thread, e.g.
 * because backing state.json was reaped), we clear our thread_id and
 * retry the POST once so the user transparently lands on a fresh
 * thread.
 */
export function useAgentStream() {
  const [status, setStatus] = useState<AgentStatus>({ kind: "idle" });
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  // threadId state mirrors localStorage; SSR-guarded initial null,
  // hydrated on mount via the useEffect below. Writes go through
  // `setLocalThreadId` which updates both this state and localStorage
  // so the next page load picks the same id up.
  const [threadId, setThreadId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Hydrate from localStorage after mount (SSR-safe).
  useEffect(() => {
    const id = getThreadId();
    if (id) setThreadId(id);
  }, []);

  const setLocalThreadId = useCallback((id: string) => {
    setThreadId(id);
    persistThreadId(id);
  }, []);

  const cleanup = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  useEffect(() => () => cleanup(), [cleanup]);

  const reset = useCallback(() => {
    cleanup();
    setStatus({ kind: "idle" });
    setTurns([]);
    setProgress(null);
    setThreadId(null);
    clearThreadId();
  }, [cleanup]);

  /**
   * Cancel the in-flight turn. Fires
   * `DELETE /agent/turn/{thread_id}` so the server can cancel the
   * iterating asyncio task (closes the codex session, aborts the
   * claim drain, releases the snapshot lease) then aborts the local
   * fetch so the React state stops updating from a stream the
   * server is about to close. Idempotent: 404 from the server
   * (turn already finished) is swallowed silently  the UI doesn't
   * care whether the cancel hit a live turn or beat the natural
   * exit by milliseconds.
   */
  const stop = useCallback(() => {
    const tid = threadId;
    cleanup();
    if (tid) {
      void fetch(`${agentUrl()}/agent/turn/${tid}`, {
        method: "DELETE",
      }).catch(() => {
        // Network errors during stop are harmless: the local
        // AbortController already closed the SSE consumer. If the
        // server keeps generating frames briefly they go nowhere.
      });
    }
    setStatus({ kind: "idle" });
    setProgress(null);
  }, [cleanup, threadId]);

  const ask = useCallback(
    async (request: AgentRequest) => {
      cleanup();
      setProgress(null);

      // Optimistically push the user's turn so the UI shows their
      // message immediately. The matching claim slots in below as
      // soon as the SSE stream produces it.
      const turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const newTurn: ChatTurn = {
        id: turnId,
        userText: request.userQuestion,
        sentAtMs: Date.now(),
        claim: null,
        narrative: null,
        narrativeProvenance: [],
        narrativeRetractedReason: null,
        narrativeRetractedDebug: null,
        error: null,
        errorDebug: null,
        gatePaths: [],
        diffReply: null,
      };
      setTurns((prev) => [...prev, newTurn]);
      setStatus({ kind: "sending" });

      const markTurnError = (
        id: string,
        msg: string,
        debug: string | null = null,
      ) => {
        setTurns((prev) => {
          const idx = prev.findIndex((t) => t.id === id);
          if (idx === -1) return prev;
          const next = prev.slice();
          if (next[idx].error === null) {
            next[idx] = { ...next[idx], error: msg, errorDebug: debug };
          }
          return next;
        });
      };

      // Build the request body, optionally carrying the saved
      // thread_id so the server resumes the conversation. Runtime
      // is sourced from the persisted RuntimeSelector store; the
      // backend ignores it on resume (already-locked runtime wins)
      // but uses it on first turn to write `runtime.json`.
      const runtime = useRuntimeSelector.getState().runtime;
      const buildBody = (tid: string | null): string => {
        const req = create(AgentRequestSchema, {
          ...request,
          threadId: tid ?? undefined,
          runtime,
        });
        return toJsonString(AgentRequestSchema, req);
      };

      // Open the streaming POST. On a 404 (stale thread) clear local
      // thread_id and retry once; on a true error mark the turn and
      // bail.
      let resp: Response;
      try {
        const controller = new AbortController();
        abortRef.current = controller;
        resp = await fetch(`${agentUrl()}/agent/turn`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: buildBody(threadId),
          signal: controller.signal,
        });
        if (resp.status === 404 && threadId) {
          // Stale thread id (server reaped its state.json or never
          // saw this id). Retry once without it so the server mints
          // a fresh thread; the user just sees "starting fresh."
          clearThreadId();
          setThreadId(null);
          resp = await fetch(`${agentUrl()}/agent/turn`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: buildBody(null),
            signal: controller.signal,
          });
        }
        if (resp.status === 409) {
          // Thread is busy with another in-flight turn. Don't
          // retry  surface a clear error so the user knows to
          // click Stop. Avoiding a retry here also keeps the
          // Stop-then-Send race from accidentally double-firing.
          const msg =
            "thread is busy with another turn. click stop to cancel it first.";
          markTurnError(turnId, msg);
          setStatus({ kind: "error", message: msg });
          cleanup();
          return;
        }
        if (!resp.ok || !resp.body) {
          const message = await resp.text().catch(() => `${resp.status}`);
          markTurnError(turnId, `turn failed: ${resp.status} ${message}`);
          setStatus({
            kind: "error",
            message: `turn failed: ${resp.status} ${message}`,
          });
          cleanup();
          return;
        }
      } catch (e) {
        const msg = `turn failed: ${e instanceof Error ? e.message : String(e)}`;
        markTurnError(turnId, msg);
        setStatus({ kind: "error", message: msg });
        cleanup();
        return;
      }

      // Server echoes the (possibly minted) thread_id in a response
      // header so we can persist before draining the SSE body.
      const serverThreadId = resp.headers.get("x-mca-thread-id");
      if (serverThreadId) {
        setLocalThreadId(serverThreadId);
      }
      const activeThreadId = serverThreadId ?? threadId ?? "";
      setStatus({ kind: "streaming", threadId: activeThreadId });

      // Drain the SSE stream until the upstream closes (Done frame)
      // or the AbortController fires (cleanup / new turn / unmount).
      const updateTurn = (mut: (t: ChatTurn) => ChatTurn) => {
        setTurns((prev) => {
          const idx = prev.findIndex((t) => t.id === turnId);
          if (idx === -1) return prev;
          const next = prev.slice();
          next[idx] = mut(next[idx]);
          return next;
        });
      };

      try {
        for await (const evt of parseSseStream(resp.body)) {
          dispatchEvent(evt.event, evt.data);
        }
      } catch (e) {
        if ((e as { name?: string })?.name === "AbortError") {
          // User-initiated cancel; status is already updated.
          return;
        }
        const msg = `stream interrupted: ${e instanceof Error ? e.message : String(e)}`;
        markTurnError(turnId, msg);
        setStatus({ kind: "error", message: msg });
        cleanup();
        return;
      }

      function dispatchEvent(name: string, data: string) {
        if (name === "Claim") {
          try {
            const claim = fromJsonString(ClaimSchema, data);
            updateTurn((t) => (t.claim === null ? { ...t, claim } : t));
          } catch {
            // skip malformed payloads in v0
          }
          return;
        }
        if (name === "Progress") {
          try {
            const evt = JSON.parse(data) as ProgressEvent;
            setProgress(evt);
          } catch {
            // ignore
          }
          return;
        }
        if (name === "NarrativeDelta") {
          // Chunk 3.5: codex emits prose token-by-token via
          // NarrativeDelta. Accumulate into t.narrative so the
          // bubble fills in real time; provenance arrives later
          // via the terminal Narrative frame.
          try {
            const parsed = fromJsonString(NarrativeDeltaSchema, data);
            const chunk = parsed.text ?? "";
            if (!chunk) return;
            updateTurn((t) => ({
              ...t,
              narrative: (t.narrative ?? "") + chunk,
            }));
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "Narrative") {
          try {
            const parsed = fromJsonString(NarrativeWithRefsSchema, data);
            const text = parsed.text ?? "";
            const provenance = parsed.provenance ?? [];
            if (!text) return;
            // Always set provenance on the terminal frame. If
            // streaming deltas already filled `narrative`, replace
            // with the canonical final text the constitution gate
            // approved (matches what's been streamed in the happy
            // path; differs only if the gate altered the text).
            updateTurn((t) => ({
              ...t,
              narrative: text,
              narrativeProvenance: provenance,
            }));
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "NarrativeRetracted") {
          try {
            const parsed = fromJsonString(NarrativeRetractedSchema, data);
            const text = parsed.text ?? "";
            const reason = parsed.reason || "Interpretation withheld.";
            const debugReason = parsed.debugReason ?? null;
            if (!text) return;
            updateTurn((t) =>
              t.narrative === null
                ? {
                    ...t,
                    narrative: text,
                    narrativeRetractedReason: reason,
                    narrativeRetractedDebug: debugReason,
                  }
                : t,
            );
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "Error") {
          let msg =
            "Couldn't produce a valid response. Try rephrasing or try again.";
          let debug: string | null = null;
          try {
            const parsed = JSON.parse(data) as {
              message?: string;
              debugMessage?: string;
              error?: string;
            };
            if (parsed.message) msg = parsed.message;
            else if (parsed.error) msg = parsed.error;
            if (parsed.debugMessage) debug = parsed.debugMessage;
          } catch {
            // keep defaults
          }
          markTurnError(turnId, msg, debug);
          return;
        }
        if (name === "GatePath") {
          try {
            const path = fromJsonString(GatePathSchema, data);
            updateTurn((t) => ({ ...t, gatePaths: [...t.gatePaths, path] }));
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "NoMovement") {
          try {
            const payload = fromJsonString(NoMovementSchema, data);
            updateTurn((t) =>
              t.diffReply === null
                ? { ...t, diffReply: { kind: "no-movement", payload } }
                : t,
            );
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "ChangedSince") {
          try {
            const payload = fromJsonString(ChangedSinceSchema, data);
            updateTurn((t) =>
              t.diffReply === null
                ? { ...t, diffReply: { kind: "changed-since", payload } }
                : t,
            );
          } catch {
            // skip malformed payloads
          }
          return;
        }
        if (name === "Done") {
          try {
            const done = fromJsonString(AgentDoneSchema, data);
            const timings = {
              // proto3 default for an unset sub-message is a
              // zero-filled instance, so reading the fields is
              // safe even when the backend omits the message.
              primaryMs: done.roleTimings?.primaryMs ?? 0,
              policyMs: done.roleTimings?.policyMs ?? 0,
              judgeMs: done.roleTimings?.judgeMs ?? 0,
            };
            // Publish timings to the dedicated store so the
            // sibling ModelsPanel can read without prop-drilling
            // through the agent sheet.
            useRoleTimings.getState().setLatest(timings);
            setStatus({
              kind: "done",
              threadId: activeThreadId,
              elapsedMs: done.elapsedMs,
              traceId: done.traceId ?? "",
              roleTimings: timings,
            });
          } catch {
            setStatus({
              kind: "done",
              threadId: activeThreadId,
              elapsedMs: 0,
              traceId: "",
              roleTimings: { primaryMs: 0, policyMs: 0, judgeMs: 0 },
            });
          }
          // Defensive: if the loop ended without emitting anything,
          // mark the turn errored so the spinner doesn't hang.
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const t = prev[idx];
            if (
              t.claim !== null ||
              t.narrative !== null ||
              t.error !== null ||
              t.diffReply !== null
            ) {
              return prev;
            }
            const next = prev.slice();
            next[idx] = { ...t, error: "agent ended without a response" };
            return next;
          });
        }
      }
    },
    [cleanup, setLocalThreadId, threadId],
  );

  // `turn` was an explicit field on the deleted `AgentSessionStarted`
  // proto. The frontend used it only to render a chip indicator. We
  // derive it from the local turns array length now (one entry per
  // user message); preserves the existing prop shape consumers see.
  const turn = turns.length;

  return { status, turns, progress, threadId, turn, ask, reset, stop };
}

export type AgentStreamState = ReturnType<typeof useAgentStream>;
