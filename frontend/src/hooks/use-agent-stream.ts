"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentRequest } from "@/lib/generated/AgentRequest";
import type { AgentSessionStarted } from "@/lib/generated/AgentSessionStarted";
import type { Claim } from "@/lib/generated/Claim";
import type { AgentDone } from "@/lib/generated/AgentDone";
import type { ChangedSince } from "@/lib/generated/ChangedSince";
import type { GatePath } from "@/lib/generated/GatePath";
import type { NarrativeWithRefs } from "@/lib/generated/NarrativeWithRefs";
import type { NoMovement } from "@/lib/generated/NoMovement";
import type { ProvenanceRef } from "@/lib/generated/ProvenanceRef";
import type { ProgressEvent } from "@/components/agent/progress-strip";

const DEFAULT_API_URL = "http://localhost:8002";

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

export type AgentStatus =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "streaming"; sessionId: string }
  | { kind: "done"; sessionId: string; elapsedMs: number }
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
 * backend is started with `AGENT_DEBUG_PUBLIC=1`. The frontend renders
 * them inline as a small monospace block under the user-facing text
 * so the dev sees rare events on the UI itself (the only surface the
 * solo dev naturally checks). In prod the backend never populates
 * these fields, so they're always null.
 *
 * A turn may carry any combination: claim only, narrative only, both,
 * a retracted narrative, or nothing (the Done-fallback flags the last
 * case as errored so the spinner doesn't hang).
 *
 * Pending = nothing yet: claim, narrative, retraction, and error all
 * null. The "thinking..." placeholder renders only while pending.
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
   * Empty when the model emitted no audit chips (descriptive-only
   * narrative).
   */
  narrativeProvenance: ProvenanceRef[];
  narrativeRetractedReason: string | null;
  narrativeRetractedDebug: string | null;
  error: string | null;
  errorDebug: string | null;
  /**
   * Ship 3.5 builder-view trace. Only populated when the request
   * was sent with `show_trace: true`. One entry per channel
   * (claim / narrative); a turn may have either, both, or
   * neither.
   */
  gatePaths: GatePath[];
  /**
   * Ship 4 `dont_repeat_yourself` payload. Mutually exclusive
   * with the normal claim/narrative path (a turn that took the
   * diff path has diffReply set and claim/narrative null). The
   * renderer uses this to show "no movement since turn N" or
   * "changed since turn N: X" bubble in place of the regular
   * claim/narrative.
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
 * id). The backend mints/echoes a `thread_id` on the POST response;
 * we keep it across turns. `reset()` clears it ("new" button); page
 * refresh drops it (component unmount).
 *
 * Lifted to `graph-page.tsx` so the hook state survives the agent
 * sheet closing + reopening.
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
      // message immediately. The matching claim slots in below it as
      // soon as the SSE stream produces it; until then, the turn
      // renders a "thinking..." placeholder.
      const turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const newTurn: ChatTurn = {
        id: turnId,
        userText: request.user_question,
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

      const requestWithThread: AgentRequest = {
        ...request,
        thread_id: threadId,
      };

      let sessionStart: AgentSessionStarted;
      try {
        const res = await fetch(`${apiUrl()}/agent/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestWithThread),
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
        sessionStart = (await res.json()) as AgentSessionStarted;
      } catch (e) {
        const msg = `ask failed: ${e instanceof Error ? e.message : String(e)}`;
        markTurnError(turnId, msg);
        setStatus({ kind: "error", message: msg });
        return;
      }

      const sessionId = sessionStart.session_id;
      setThreadId(sessionStart.thread_id);
      setTurn(sessionStart.turn);
      setStatus({ kind: "streaming", sessionId });

      const es = new EventSource(`${apiUrl()}/agent/stream/${sessionId}`);
      eventSourceRef.current = es;

      es.addEventListener("Claim", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const claim = JSON.parse(data) as Claim;
          // Attach the claim to this turn (the latest pending one).
          // If multiple Claim events arrive for the same turn (a
          // future possibility), only the first attaches; subsequent
          // claims would need a list shape, which we don't have yet.
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
          const evt = JSON.parse(data) as ProgressEvent;
          setProgress(evt);
        } catch {
          // ignore
        }
      });

      es.addEventListener("Narrative", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          // Ship 5a wire shape: NarrativeWithRefs { text, provenance }.
          // `provenance` is the assembled typed citation array used
          // by the bubble to render `${ref:N}` chips.
          const parsed = JSON.parse(data) as Partial<NarrativeWithRefs>;
          const text = parsed.text ?? "";
          const provenance = parsed.provenance ?? [];
          if (!text) return;
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            // First Narrative wins for a turn. Future ships could
            // append (a streamed-token shape), but ship 1.6 sends
            // one frame per turn.
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
          const parsed = JSON.parse(data) as {
            text?: string;
            reason?: string;
            debug_reason?: string;
          };
          const text = parsed.text ?? "";
          const reason = parsed.reason ?? "Interpretation withheld.";
          const debugReason = parsed.debug_reason ?? null;
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
          const parsed = JSON.parse(data) as {
            message?: string;
            debug_message?: string;
          };
          if (parsed.message) msg = parsed.message;
          if (parsed.debug_message) debug = parsed.debug_message;
        } catch {
          // keep defaults
        }
        markTurnError(turnId, msg, debug);
      });

      es.addEventListener("GatePath", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        try {
          const path = JSON.parse(data) as GatePath;
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
          const payload = JSON.parse(data) as NoMovement;
          setTurns((prev) => {
            const idx = prev.findIndex((t) => t.id === turnId);
            if (idx === -1) return prev;
            const next = prev.slice();
            // Only attach if no diffReply yet AND this turn hasn't
            // already rendered a regular claim/narrative.
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
          const payload = JSON.parse(data) as ChangedSince;
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
          const done = JSON.parse(data) as AgentDone;
          setStatus({
            kind: "done",
            sessionId: done.session_id,
            elapsedMs: done.elapsed_ms,
          });
        } catch {
          setStatus({ kind: "done", sessionId, elapsedMs: 0 });
        }
        // Defensive: if the loop ended without emitting anything at
        // all (no Claim, no Narrative) and no explicit Error frame,
        // the turn would otherwise hang on "thinking..." forever.
        // Mark it errored so the user sees something. Narrative-only
        // turns are valid (interpretive replies) and short-circuit
        // here.
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
