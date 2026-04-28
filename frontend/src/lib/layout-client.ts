/**
 * Indirection between the React hook and the runtime that owns
 * physics state. The interface is a delta-protocol: main calls
 * `addNode`/`addEdge`/etc as edges arrive on the SSE stream, and
 * `applyLatestPositions` once per frame to write whatever the engine
 * has produced onto the graphology graph Sigma renders from.
 *
 * Two implementations:
 *   - In-process: holds a `LayoutEngine` and runs `step()`
 *     synchronously inside `applyLatestPositions`. Same code path as
 *     before the worker move.
 *   - Worker: posts each delta as a typed message; the worker holds
 *     its own `LayoutEngine` and ticks on its own RAF; positions are
 *     transferred back as `Float64Array`s and written to graphology
 *     in `applyLatestPositions`.
 *
 * Positions flow one way: engine → main → graphology → Sigma. They
 * never round-trip back into the engine. This avoids the
 * double-integration drift that broke the previous worker design.
 */
import type Graph from "graphology";

import { LayoutEngine } from "@/lib/layout-engine";
import type { NodeRole } from "@/lib/role-detect";

export interface LayoutClient {
  addNode(slot: number, x: number, y: number): void;
  addEdge(srcSlot: number, dstSlot: number, weight: number): void;
  bumpEdgeWeight(srcSlot: number, dstSlot: number, delta: number): void;
  removeNode(slot: number): void;
  removeEdge(srcSlot: number, dstSlot: number): void;
  setRole(slot: number, role: NodeRole): void;
  setCommunity(slot: number, community: number): void;
  /** Flush the engine's latest positions onto graphology. Called
   *  once per main RAF. */
  applyLatestPositions(
    graph: Graph,
    pubkeyBySlot: Map<number, string>,
  ): void;
  reset(): void;
  terminate?(): void;
}

export function createInProcessLayoutClient(): LayoutClient {
  const engine = new LayoutEngine();
  return {
    addNode: (slot, x, y) => engine.addNode(slot, x, y),
    addEdge: (s, d, w) => engine.addEdge(s, d, w),
    bumpEdgeWeight: (s, d, w) => engine.bumpEdgeWeight(s, d, w),
    removeNode: (slot) => engine.removeNode(slot),
    removeEdge: (s, d) => engine.removeEdge(s, d),
    setRole: (slot, role) => engine.setRole(slot, role),
    setCommunity: (slot, c) => engine.setCommunity(slot, c),
    applyLatestPositions: (graph, pubkeyBySlot) => {
      engine.step();
      engine.forEachChangedPosition((slot, x, y) => {
        const pubkey = pubkeyBySlot.get(slot);
        if (pubkey === undefined) return;
        if (!graph.hasNode(pubkey)) return;
        graph.setNodeAttribute(pubkey, "x", x);
        graph.setNodeAttribute(pubkey, "y", y);
      });
    },
    reset: () => engine.reset(),
  };
}

export function createWorkerLayoutClient(): LayoutClient {
  const worker = new Worker(
    new URL("./layout-client.worker.ts", import.meta.url),
    { type: "module" },
  );

  // Per-slot pending positions. Worker emits diffs (only slots that
  // moved since its last post); we merge each diff into this map so
  // a fast burst of worker posts before main's next RAF doesn't
  // drop any slot's update  the latest x/y per slot wins.
  // Drained by `applyLatestPositions` once per RAF.
  const pending = new Map<number, [number, number]>();

  worker.onmessage = (ev: MessageEvent) => {
    const data = ev.data;
    if (!data || data.type !== "positions") return;
    const slots = data.slots as Uint32Array;
    const xs = data.xs as Float64Array;
    const ys = data.ys as Float64Array;
    for (let i = 0; i < slots.length; i++) {
      pending.set(slots[i], [xs[i], ys[i]]);
    }
  };

  worker.onerror = (ev: ErrorEvent) => {
    // eslint-disable-next-line no-console
    console.error("[layout-worker] error:", ev.message ?? ev);
  };

  // Outgoing delta buffer. Every `add*` / `remove*` / `set*` call
  // pushes here instead of posting to the worker. A single
  // `applyDeltas` message ships the whole buffer once per RAF, so
  // 800-1200 individual postMessages per second (at sustained
  // 405 tx/sec) collapse to 60.
  //
  // Bootstrap (the SSE replay of an entire window when a fresh
  // connection opens) can produce 10k-30k deltas in a single tick.
  // Cloning that many objects across postMessage in one shot is
  // 200-500ms of main-thread blocking, which presents as a visible
  // freeze. `MAX_BATCH` triggers an immediate flush whenever the
  // buffer crosses the cap so bootstrap ships as several medium
  // messages instead of one huge one. Steady-state ingestion is far
  // below the cap so it never trips during normal flow.
  const MAX_BATCH = 2000;
  let outBuffer: unknown[] = [];
  let flushScheduled = false;

  const flushDeltas = () => {
    flushScheduled = false;
    if (outBuffer.length === 0) return;
    const batch = outBuffer;
    outBuffer = [];
    worker.postMessage({ type: "applyDeltas", deltas: batch });
  };

  const enqueue = (delta: unknown) => {
    outBuffer.push(delta);
    if (outBuffer.length >= MAX_BATCH) {
      // Synchronous flush; cancel any pending RAF flush, the buffer
      // is empty after this call.
      flushScheduled = false;
      flushDeltas();
      return;
    }
    if (!flushScheduled) {
      flushScheduled = true;
      requestAnimationFrame(flushDeltas);
    }
  };

  return {
    addNode: (slot, x, y) => enqueue({ op: "addNode", slot, x, y }),
    addEdge: (s, d, w) =>
      enqueue({ op: "addEdge", srcSlot: s, dstSlot: d, weight: w }),
    bumpEdgeWeight: (s, d, delta) =>
      enqueue({ op: "bumpEdgeWeight", srcSlot: s, dstSlot: d, delta }),
    removeNode: (slot) => enqueue({ op: "removeNode", slot }),
    removeEdge: (s, d) =>
      enqueue({ op: "removeEdge", srcSlot: s, dstSlot: d }),
    setRole: (slot, role) => enqueue({ op: "setRole", slot, role }),
    setCommunity: (slot, community) =>
      enqueue({ op: "setCommunity", slot, community }),
    applyLatestPositions: (graph, pubkeyBySlot) => {
      if (pending.size === 0) return;
      for (const [slot, xy] of pending) {
        const pubkey = pubkeyBySlot.get(slot);
        if (pubkey === undefined) continue;
        if (!graph.hasNode(pubkey)) continue;
        graph.setNodeAttribute(pubkey, "x", xy[0]);
        graph.setNodeAttribute(pubkey, "y", xy[1]);
      }
      pending.clear();
    },
    reset: () => {
      // Drop pending deltas  they're stale relative to a fresh
      // graph. Reset is sent immediately (not batched) so the
      // worker drops its state before any post-reset deltas land.
      outBuffer = [];
      flushScheduled = false;
      pending.clear();
      worker.postMessage({ type: "reset" });
    },
    terminate: () => {
      worker.terminate();
    },
  };
}
