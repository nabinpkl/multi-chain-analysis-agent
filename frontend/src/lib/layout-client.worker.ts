/**
 * Web Worker host for the layout engine. Subscribes to delta
 * messages from main, ticks physics on its own RAF, posts position
 * snapshots back. The worker's engine is the authoritative state
 * machine; main never sends positions.
 *
 * Message protocol:
 *   main -> worker: every delta call on `LayoutClient` becomes a
 *     typed message  addNode, addEdge, bumpEdgeWeight, removeNode,
 *     removeEdge, setRole, setCommunity, reset.
 *   worker -> main: { type: "positions", slots, xs, ys } once per
 *     internal frame; transferable. Main applies the latest one to
 *     graphology (latest-only delivery, see `layout-client.ts`).
 */
import { LayoutEngine } from "./layout-engine";

const engine = new LayoutEngine();

// 30Hz physics. Render stays at the host's 60Hz because main's RAF
// just consumes the latest pending diff; physics output beyond
// ~30Hz isn't visually distinguishable for force-layout movement
// where MAX_STEP caps per-step deltas at small values, so we trade
// the upper half of physics CPU for heat budget. Worker contexts
// rarely expose `requestAnimationFrame`; `setTimeout` keeps the
// worker independent of the host compositor.
const FRAME_INTERVAL_MS = 33;

function scheduleNextTick(): void {
  setTimeout(tick, FRAME_INTERVAL_MS);
}

function tick(): void {
  try {
    engine.step();
    const snap = engine.snapshotChangedPositions();
    // Skip the postMessage on quiet ticks (no positions changed
    // since the last post). Saves the 720KB+ transfer at 30k nodes
    // and the corresponding `setNodeAttribute` storm on main.
    if (snap !== null) {
      (self as unknown as Worker).postMessage(
        { type: "positions", slots: snap.slots, xs: snap.xs, ys: snap.ys },
        [
          snap.slots.buffer as ArrayBuffer,
          snap.xs.buffer as ArrayBuffer,
          snap.ys.buffer as ArrayBuffer,
        ],
      );
    }
  } catch (err) {
    // Don't kill the loop on a single bad tick.
    // eslint-disable-next-line no-console
    console.error("[layout-worker tick]", err);
  }
  scheduleNextTick();
}

interface DeltaItem {
  op: string;
  slot?: number;
  x?: number;
  y?: number;
  srcSlot?: number;
  dstSlot?: number;
  weight?: number;
  delta?: number;
  role?: string;
  community?: number;
}

function applyDelta(d: DeltaItem): void {
  switch (d.op) {
    case "addNode":
      engine.addNode(d.slot as number, d.x as number, d.y as number);
      break;
    case "addEdge":
      engine.addEdge(
        d.srcSlot as number,
        d.dstSlot as number,
        d.weight as number,
      );
      break;
    case "bumpEdgeWeight":
      engine.bumpEdgeWeight(
        d.srcSlot as number,
        d.dstSlot as number,
        d.delta as number,
      );
      break;
    case "removeNode":
      engine.removeNode(d.slot as number);
      break;
    case "removeEdge":
      engine.removeEdge(d.srcSlot as number, d.dstSlot as number);
      break;
    case "setRole":
      engine.setRole(d.slot as number, d.role as never);
      break;
    case "setCommunity":
      engine.setCommunity(d.slot as number, d.community as number);
      break;
    default:
      // eslint-disable-next-line no-console
      console.warn("[layout-worker] unknown delta op:", d.op);
  }
}

self.onmessage = (ev: MessageEvent) => {
  const msg = ev.data;
  if (!msg || typeof msg !== "object") return;
  // `applyDeltas` is the hot path: main batches every per-RAF
  // delta into one message to amortize postMessage cost. `reset`
  // arrives out-of-band (immediate, not batched) so a window
  // switch / "Reset from now" takes effect even if there are
  // stale deltas in the queue.
  if (msg.type === "applyDeltas") {
    const deltas = msg.deltas as DeltaItem[];
    for (let i = 0; i < deltas.length; i++) {
      applyDelta(deltas[i]);
    }
    return;
  }
  if (msg.type === "reset") {
    engine.reset();
    return;
  }
  // eslint-disable-next-line no-console
  console.warn("[layout-worker] unknown message type:", msg.type);
};

scheduleNextTick();
