import type { EdgeWire } from "./generated/EdgeWire";

export type { EdgeKind } from "./generated/EdgeKind";

/** Wire shape for a single edge streamed over SSE. Generated from Rust via ts-rs. */
export type RawEdge = EdgeWire;

const DEFAULT_API_URL = "http://localhost:8002";

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

/**
 * Opens an SSE connection to the raw edge fire-hose. Every ingested
 * transaction fires one `edge` event. No snapshot, no catch-up: clients
 * see only edges that arrive after they connect. The `onLag` callback
 * fires when the broadcast buffer overruns (slow subscriber).
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
