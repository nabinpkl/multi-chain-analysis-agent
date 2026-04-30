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
 * Owns the agent thread for the lifetime of the page.
 *
 * Per ship 1.5 thread continuity: each `ask()` either starts a fresh
 * thread (no `currentThreadId`) or continues an existing one (echoes
 * the stored id). The backend mints/echoes a `thread_id` on the POST
 * response; we keep it across turns. `reset()` clears it ("new"
 * button); page refresh drops it (component unmount). The matching
 * server-side thread is named by the `thread.in_memory_only` stub.
 *
 * Lifted to `graph-page.tsx` so the hook state survives the agent
 * sheet closing + reopening.
 */
export function useAgentStream() {
  const [status, setStatus] = useState<AgentStatus>({ kind: "idle" });
  const [claims, setClaims] = useState<Claim[]>([]);
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
    setClaims([]);
    setProgress(null);
    setThreadId(null);
    setTurn(0);
  }, [cleanup]);

  const ask = useCallback(
    async (request: AgentRequest) => {
      cleanup();
      // Keep claims across turns within a thread so the UI shows the
      // running conversation. Reset on `reset()` only.
      setProgress(null);
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
          setStatus({
            kind: "error",
            message: `ask failed: ${res.status} ${message}`,
          });
          return;
        }
        sessionStart = (await res.json()) as AgentSessionStarted;
      } catch (e) {
        setStatus({
          kind: "error",
          message: `ask failed: ${e instanceof Error ? e.message : String(e)}`,
        });
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
          setClaims((prev) => [...prev, claim]);
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
        cleanup();
      });

      es.onerror = () => {
        setStatus((prev) =>
          prev.kind === "streaming"
            ? {
                kind: "error",
                message: "stream interrupted",
              }
            : prev,
        );
        cleanup();
      };
    },
    [cleanup, threadId],
  );

  return { status, claims, progress, threadId, turn, ask, reset };
}

export type AgentStreamState = ReturnType<typeof useAgentStream>;
