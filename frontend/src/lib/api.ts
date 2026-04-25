export type OverviewWindow = "15m" | "1h" | "6h" | "24h";

export interface WindowView {
  from: number;
  to: number;
  label: string;
}

export interface StatsView {
  total_volume_sol: number;
  total_txs: number;
  unique_wallets: number;
  top_wallet: string | null;
  top_wallet_volume_sol: number | null;
  tx_per_sec_recent: number;
}

export interface NodeView {
  id: string;
  volume_sol: number;
  component: number | null;
  degree: number;
  x: number;
  y: number;
}

export interface EdgeView {
  from: string;
  to: string;
  volume_sol: number;
  tx_count: number;
}

export interface OverviewResponse {
  window: WindowView;
  stats: StatsView;
  nodes: NodeView[];
  edges: EdgeView[];
  generated_at: number;
  is_partial: boolean;
}

const DEFAULT_API_URL = "http://localhost:8002";

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

export async function fetchOverview(
  window: OverviewWindow,
  signal?: AbortSignal,
): Promise<OverviewResponse> {
  const url = new URL("/graph/overview", apiUrl());
  url.searchParams.set("window", window);
  const res = await fetch(url.toString(), { signal });
  if (!res.ok) {
    throw new Error(`overview request failed: ${res.status}`);
  }
  return res.json();
}

/**
 * Opens an SSE connection to the overview stream for a specific window.
 * `onSnapshot` fires on every `snapshot` event (initial + each tick where
 * state changed). The returned cleanup function closes the connection.
 */
export function subscribeOverviewStream(
  window: OverviewWindow,
  onSnapshot: (snap: OverviewResponse) => void,
  onError: (err: Event) => void,
): () => void {
  const url = new URL("/graph/overview/stream", apiUrl());
  url.searchParams.set("window", window);
  const es = new EventSource(url.toString());

  es.addEventListener("snapshot", (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as OverviewResponse;
      onSnapshot(data);
    } catch {
      // ignore malformed events
    }
  });

  es.addEventListener("resync", () => {
    // Trigger a REST-path refetch via caller's error handler path  the
    // caller can react by calling fetchOverview once to re-sync state.
    onError(new Event("resync"));
  });

  es.onerror = (ev) => {
    onError(ev);
  };

  return () => {
    es.close();
  };
}

export interface RawEdge {
  signature: string;
  block_time: number;
  from: string;
  to: string;
  volume_sol: number;
}

/**
 * Opens an SSE connection to the raw edge fire-hose. Every ingested
 * transaction fires one `edge` event. No snapshot, no catch-up: clients
 * see only edges that arrive after they connect. The `onLag` callback
 * fires when the broadcast buffer overruns (slow subscriber); the gap
 * is permanent  there is no snapshot to replay.
 */
export function subscribeRawStream(
  onEdge: (edge: RawEdge) => void,
  onLag: (missed: number) => void,
  onError: (err: Event) => void,
): () => void {
  const url = new URL("/graph/raw/stream", apiUrl());
  const es = new EventSource(url.toString());

  es.addEventListener("edge", (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as RawEdge;
      onEdge(data);
    } catch {
      // ignore malformed events
    }
  });

  es.addEventListener("lag", (ev) => {
    const m = (ev as MessageEvent).data.match(/missed (\d+)/);
    onLag(m ? parseInt(m[1], 10) : 0);
  });

  es.onerror = (ev) => {
    onError(ev);
  };

  return () => {
    es.close();
  };
}
