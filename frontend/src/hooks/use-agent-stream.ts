"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentRequest } from "@/lib/generated/AgentRequest";
import type { AgentSessionStarted } from "@/lib/generated/AgentSessionStarted";
import type { Claim } from "@/lib/generated/Claim";
import type { AgentDone } from "@/lib/generated/AgentDone";
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
 * claim (or null while the agent is still working). Renders inline as
 * a user card followed by either the claim card or a "thinking..."
 * placeholder. Future ships may bundle multiple claims per turn (e.g.
 * a Profile + a Summary); the shape is `claim` singular for v0 with
 * `error` carrying any failure message instead.
 */
export interface ChatTurn {
  id: string;
  userText: string;
  sentAtMs: number;
  claim: Claim | null;
  error: string | null;
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
        error: null,
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

      es.addEventListener("Error", (ev) => {
        const data = (ev as MessageEvent<string>).data;
        let msg = "agent loop errored";
        try {
          const parsed = JSON.parse(data) as { message?: string };
          if (parsed.message) msg = parsed.message;
        } catch {
          // keep default
        }
        markTurnError(turnId, msg);
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
        // Defensive: if the loop ended without emitting a claim and
        // without an explicit Error frame, the turn would otherwise
        // hang on "thinking..." forever. Mark it errored so the user
        // sees something.
        setTurns((prev) => {
          const idx = prev.findIndex((t) => t.id === turnId);
          if (idx === -1) return prev;
          const t = prev[idx];
          if (t.claim !== null || t.error !== null) return prev;
          const next = prev.slice();
          next[idx] = { ...t, error: "agent ended without a claim" };
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

      function markTurnError(id: string, msg: string) {
        setTurns((prev) => {
          const idx = prev.findIndex((t) => t.id === id);
          if (idx === -1) return prev;
          const next = prev.slice();
          if (next[idx].error === null) {
            next[idx] = { ...next[idx], error: msg };
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
