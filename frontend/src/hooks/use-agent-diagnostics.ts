"use client";

import { useEffect, useState } from "react";
import type { AgentDiagnostics } from "@/lib/generated/AgentDiagnostics";

const DEFAULT_API_URL = "http://localhost:8002";
const POLL_INTERVAL_MS = 10_000;

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

/**
 * Polls `GET /agent/diagnostics` for the current stub state +
 * registered primitive list. Refreshes every 10s while the agent
 * sheet is open (or whenever `enabled` is true). The stub banner
 * reads this so the user always sees what's currently stubbed.
 */
export function useAgentDiagnostics(enabled: boolean) {
  const [diagnostics, setDiagnostics] = useState<AgentDiagnostics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const res = await fetch(`${apiUrl()}/agent/diagnostics`);
        if (!res.ok) throw new Error(`status ${res.status}`);
        const body = (await res.json()) as AgentDiagnostics;
        if (!cancelled) {
          setDiagnostics(body);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    };
    fetchOnce();
    const id = setInterval(fetchOnce, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled]);

  return { diagnostics, error };
}
