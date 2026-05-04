"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { create, fromJsonString, toJsonString } from "@bufbuild/protobuf";

import {
  AgentDoneSchema,
  AgentRequestSchema,
  AgentSessionStartedSchema,
  type AgentRequest,
  type AgentSessionStarted,
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
  NarrativeRetractedSchema,
  NarrativeWithRefsSchema,
} from "@/lib/wire/multichain/wire/agent/v1/narrative_pb";
import { type ProvenanceRef } from "@/lib/wire/multichain/wire/shared/v1/provenance_pb";

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
  | { kind: "streaming"; sessionId: string }
  | {
      kind: "done";
      sessionId: string;
      elapsedMs: number;
      /**
       * 32-hex-char OTel trace id stamped on the Done frame by the
       * loop driver (Ship 1 of agent-observability, ADR 13). The
       * agent sheet renders a deep-link to Langfuse for this trace.
       * Empty string when telemetry is disabled.
       */
      traceId: string;
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
 * Owns the agent thread for the lifetime of the page.
 *
 * Per ship 1.5: each `ask()` either starts a fresh thread (no
 * `currentThreadId`) or continues an existing one (echoes the stored
 * id). The backend mints/echoes a `threadId` on the POST response;
 * we keep it across turns. `reset()` clears it ("new" button); page
 * refresh drops it (component unmount).
 */
export function useAgentStream() {
  const [status, setStatus] = useState<AgentStatus>({ kind: "idle" });
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [turn, setTurn] = useState<number>(0);
  const eventSourceRef = useRef<EventSource | null>(null);

  const cleanup = useCallback(() => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
  }, []);

  useEffect(() => () => cleanup(), [cleanup]);

  const reset = useCallback(() => {
    cleanup();
    setStatus({ kind: "idle" });
    setTurns([]);
    setProgress(null);
    setThreadId(null);
    setTurn(0);
  }, [cleanup]);

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

      const requestWithThread = create(AgentRequestSchema, {
        ...request,
        threadId: threadId ?? undefined,
      });

      let sessionStart: AgentSessionStarted;
      try {
        const res = await fetch(`${agentUrl()}/agent/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: toJsonString(AgentRequestSchema, requestWithThread),
        });
        if (!res.ok) {
          const message = await res.text();
          markTurnError(turnId, `ask failed: ${res.status} ${message}`);
          setStatus({
            kind: "error",
            message: `ask failed: ${res.status} ${message}`,
          });
          return;
        }
        sessionStart = fromJsonString(AgentSessionStartedSchema, await res.text());
      } catch (e) {
        const msg = `ask failed: ${e instanceof Error ? e.message : String(e)}`;
        markTurnError(turnId, msg);
        setStatus({ kind: "error", message: msg });
        return;
      }

      const sessionId = sessionStart.sessionId;
      setThreadId(sessionStart.threadId);
      setTurn(sessionStart.turn);
      setStatus({ kind: "streaming", sessionId });

      const es = new EventSource(`${agentUrl()}/agent/stream/${sessionId}`);
      eventSourceRef.current = es;

      es.addEventListener("Claim", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const claim = fromJsonString(ClaimSchema, data);
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            if (next[idx].claim === null) {
              next[idx] = { ...next[idx], claim };
            }
            return next;
          });
        } catch {
          // skip malformed payloads in v0
        }
      });

      es.addEventListener("Progress", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          // Progress is a tiny wire shape ({phase, detail}); the
          // ProgressEvent UI type accepts any string phase, so a
          // direct JSON.parse is fine here.
          const evt = JSON.parse(data) as ProgressEvent;
          setProgress(evt);
        } catch {
          // ignore
        }
      });

      es.addEventListener("Narrative", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const parsed = fromJsonString(NarrativeWithRefsSchema, data);
          const text = parsed.text ?? "";
          const provenance = parsed.provenance ?? [];
          if (!text) return;
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            // First Narrative wins for a turn.
            if (next[idx].narrative === null) {
              next[idx] = {
                ...next[idx],
                narrative: text,
                narrativeProvenance: provenance,
              };
            }
            return next;
          });
        } catch {
          // skip malformed payloads
        }
      });

      es.addEventListener("NarrativeRetracted", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const parsed = fromJsonString(NarrativeRetractedSchema, data);
          const text = parsed.text ?? "";
          const reason = parsed.reason || "Interpretation withheld.";
          const debugReason = parsed.debugReason ?? null;
          if (!text) return;
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            if (next[idx].narrative === null) {
              next[idx] = {
                ...next[idx],
                narrative: text,
                narrativeRetractedReason: reason,
                narrativeRetractedDebug: debugReason,
              };
            }
            return next;
          });
        } catch {
          // skip malformed payloads
        }
      });

      es.addEventListener("Error", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        let msg = "Couldn't produce a valid response. Try rephrasing or try again.";
        let debug: string | null = null;
        try {
          // Error frames may also be naive JSON {"error","kind"}
          // (Python proxies the Rust error shape). Tolerate both.
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
      });

      es.addEventListener("GatePath", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const path = fromJsonString(GatePathSchema, data);
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            next[idx] = {
              ...next[idx],
              gatePaths: [...next[idx].gatePaths, path],
            };
            return next;
          });
        } catch {
          // skip malformed payloads
        }
      });

      es.addEventListener("NoMovement", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const payload = fromJsonString(NoMovementSchema, data);
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            if (next[idx].diffReply === null) {
              next[idx] = {
                ...next[idx],
                diffReply: { kind: "no-movement", payload },
              };
            }
            return next;
          });
        } catch {
          // skip malformed payloads
        }
      });

      es.addEventListener("ChangedSince", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const payload = fromJsonString(ChangedSinceSchema, data);
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            if (next[idx].diffReply === null) {
              next[idx] = {
                ...next[idx],
                diffReply: { kind: "changed-since", payload },
              };
            }
            return next;
          });
        } catch {
          // skip malformed payloads
        }
      });

      es.addEventListener("Done", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const done = fromJsonString(AgentDoneSchema, data);
          setStatus({
            kind: "done",
            sessionId: done.sessionId,
            elapsedMs: done.elapsedMs,
            traceId: done.traceId ?? "",
          });
        } catch {
          setStatus({ kind: "done", sessionId, elapsedMs: 0, traceId: "" });
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
        cleanup();
      });

      es.onerror = () => {
        setStatus((prev) => {
          if (prev.kind === "streaming") {
            markTurnError(turnId, "stream interrupted");
            return { kind: "error", message: "stream interrupted" };
          }
          return prev;
        });
        cleanup();
      };

      function markTurnError(id: string, msg: string, debug: string | null = null) {
        setTurns((prev) => {
          const idx = prev.findIndex((t) => t.id === id);
          if (idx === -1) return prev;
          const next = prev.slice();
          if (next[idx].error === null) {
            next[idx] = { ...next[idx], error: msg, errorDebug: debug };
          }
          return next;
        });
      }
    },
    [cleanup, threadId],
  );

  return { status, turns, progress, threadId, turn, ask, reset };
}

export type AgentStreamState = ReturnType<typeof useAgentStream>;
