/**
 * Stateful layout engine. Subscribes to a delta stream (addNode,
 * addEdge, bumpEdgeWeight, removeNode, removeEdge, setRole,
 * setCommunity) and produces positions on demand. Internal state is
 * keyed by integer slot ids assigned by the caller.
 *
 * The engine reuses `stepLayout` from `per-component-layout.ts` for
 * the actual physics; it builds a `LayoutSnapshot` from its own
 * Map-backed graph mirror each step. No graphology dependency.
 *
 * Designed so the same instance runs in two contexts:
 *   - In-process (main thread, called from the React hook).
 *   - Inside a Web Worker, with the same delta inputs marshalled
 *     across postMessage.
 *
 * Positions are owned here. The caller (graphology in the hook,
 * Sigma in the renderer) consumes them via `forEachPosition` once
 * per render. Positions never round-trip back into the engine.
 */
import {
  addNode as ufAddNode,
  createComponentState,
  findRoot,
  removeNode as ufRemoveNode,
  union,
  type ComponentState,
} from "@/lib/components";
import {
  createLayoutState,
  resetLayoutState,
  stepLayout,
  type LayoutSnapshot,
  type LayoutState,
} from "@/lib/per-component-layout";
import { teleportToAnchor } from "@/lib/spawn-helpers";
import type { NodeRole } from "@/lib/role-detect";

const NODE_SIZE_MIN_PX = 1.5;
const NODE_SIZE_MAX_PX = 10;
const NODE_SIZE_REF_DEGREE = 60;

/** Identical to `nodeSize` in `use-raw-stream.ts`. Duplicated so
 *  the engine doesn't depend on graphology, and so size matches main
 *  whether main writes it explicitly via `setSize` or the engine
 *  computes it from degree. */
function nodeSizeFromDegree(degree: number): number {
  if (degree <= 1) return 0.8;
  const norm = Math.min(
    1,
    (degree - 1) / (NODE_SIZE_REF_DEGREE - 1),
  );
  return (
    NODE_SIZE_MIN_PX + Math.sqrt(norm) * (NODE_SIZE_MAX_PX - NODE_SIZE_MIN_PX)
  );
}

interface NodeRecord {
  x: number;
  y: number;
  /** Last x reported via `snapshotPositions`. `NaN` until the first
   *  successful post. Used to compute the position diff so the
   *  worker only ships slots whose position actually changed. */
  lastReportedX: number;
  lastReportedY: number;
  degree: number;
  size: number;
  isTip: boolean;
  community: number;
}

export class LayoutEngine {
  private nodes = new Map<number, NodeRecord>();
  // src slot -> (dst slot -> weight). Symmetric (we duplicate both
  // directions on add/remove for O(1) lookup either way).
  private adj = new Map<number, Map<number, number>>();
  private uf: ComponentState = createComponentState();
  // Roots of components touched since the last successful step.
  private dirty = new Set<string>();
  private layoutState: LayoutState = createLayoutState();

  // ---- Delta inputs ------------------------------------------------

  addNode(slot: number, x: number, y: number): void {
    if (this.nodes.has(slot)) return;
    this.nodes.set(slot, {
      x,
      y,
      // Force first snapshot to include this node by leaving last
      // reported as NaN  any real x/y compares unequal.
      lastReportedX: Number.NaN,
      lastReportedY: Number.NaN,
      degree: 0,
      size: nodeSizeFromDegree(0),
      isTip: false,
      community: -1,
    });
    this.adj.set(slot, new Map());
    ufAddNode(this.uf, String(slot));
    this.dirty.add(findRoot(this.uf, String(slot)));
  }

  addEdge(srcSlot: number, dstSlot: number, weight: number): void {
    if (srcSlot === dstSlot) return;
    const srcAdj = this.adj.get(srcSlot);
    const dstAdj = this.adj.get(dstSlot);
    const src = this.nodes.get(srcSlot);
    const dst = this.nodes.get(dstSlot);
    if (!srcAdj || !dstAdj || !src || !dst) return;
    if (srcAdj.has(dstSlot)) return; // already exists
    srcAdj.set(dstSlot, weight);
    dstAdj.set(srcSlot, weight);
    src.degree += 1;
    dst.degree += 1;
    src.size = nodeSizeFromDegree(src.degree);
    dst.size = nodeSizeFromDegree(dst.degree);

    const merge = union(this.uf, String(srcSlot), String(dstSlot));
    if (merge.merged) {
      // Teleport the migrated members of the smaller component to
      // sit around the surviving anchor. Exact same formula main
      // would use for graphology (see `spawn-helpers.ts`), so the
      // worker's positions stay in lockstep with main's view.
      const anchorSlot = Number(merge.winner);
      const anchorPos = this.nodes.get(anchorSlot);
      if (anchorPos) {
        for (const memberStr of merge.migrated) {
          if (memberStr === merge.winner) continue;
          const slot = Number(memberStr);
          const node = this.nodes.get(slot);
          if (!node) continue;
          const tp = teleportToAnchor(memberStr, anchorPos);
          node.x = tp.x;
          node.y = tp.y;
        }
      }
    }
    this.dirty.add(findRoot(this.uf, String(srcSlot)));
  }

  bumpEdgeWeight(srcSlot: number, dstSlot: number, delta: number): void {
    if (srcSlot === dstSlot) return;
    const srcAdj = this.adj.get(srcSlot);
    const dstAdj = this.adj.get(dstSlot);
    if (!srcAdj || !dstAdj) return;
    const cur = srcAdj.get(dstSlot);
    if (cur === undefined) return;
    const next = cur + delta;
    srcAdj.set(dstSlot, next);
    dstAdj.set(srcSlot, next);
    this.dirty.add(findRoot(this.uf, String(srcSlot)));
  }

  removeNode(slot: number): void {
    const adj = this.adj.get(slot);
    if (adj) {
      for (const other of adj.keys()) {
        const otherAdj = this.adj.get(other);
        otherAdj?.delete(slot);
        const otherNode = this.nodes.get(other);
        if (otherNode) {
          otherNode.degree = Math.max(0, otherNode.degree - 1);
          otherNode.size = nodeSizeFromDegree(otherNode.degree);
        }
      }
      this.adj.delete(slot);
    }
    this.nodes.delete(slot);
    ufRemoveNode(this.uf, String(slot));
    // Conservative: don't dirty anything (the component the node
    // belonged to will be picked up by other deltas).
  }

  removeEdge(srcSlot: number, dstSlot: number): void {
    if (srcSlot === dstSlot) return;
    const srcAdj = this.adj.get(srcSlot);
    const dstAdj = this.adj.get(dstSlot);
    if (!srcAdj || !dstAdj) return;
    if (!srcAdj.has(dstSlot)) return;
    srcAdj.delete(dstSlot);
    dstAdj.delete(srcSlot);
    const src = this.nodes.get(srcSlot);
    const dst = this.nodes.get(dstSlot);
    if (src) {
      src.degree = Math.max(0, src.degree - 1);
      src.size = nodeSizeFromDegree(src.degree);
    }
    if (dst) {
      dst.degree = Math.max(0, dst.degree - 1);
      dst.size = nodeSizeFromDegree(dst.degree);
    }
    // Note: we don't run split-detection in the engine. The frontend
    // UF doesn't support split (matches main's behavior). Worst case:
    // pushComponentsApart treats two disconnected sub-clusters as one
    // until a future merge restructures things. Visually acceptable.
    this.dirty.add(findRoot(this.uf, String(srcSlot)));
  }

  setRole(slot: number, role: NodeRole): void {
    const node = this.nodes.get(slot);
    if (!node) return;
    const isTip = role === "tip-account";
    if (node.isTip !== isTip) {
      node.isTip = isTip;
      this.dirty.add(findRoot(this.uf, String(slot)));
    }
  }

  setCommunity(slot: number, community: number): void {
    const node = this.nodes.get(slot);
    if (!node) return;
    if (node.community !== community) {
      node.community = community;
      this.dirty.add(findRoot(this.uf, String(slot)));
    }
  }

  reset(): void {
    this.nodes.clear();
    this.adj.clear();
    this.uf = createComponentState();
    this.dirty.clear();
    resetLayoutState(this.layoutState);
  }

  // ---- Tick + position output -------------------------------------

  /** Advance physics by one step over dirty components. No-op if no
   *  deltas have arrived since the previous call. */
  step(): void {
    if (this.dirty.size === 0) return;
    const snap = this.buildSnapshot();
    if (snap === null) {
      this.dirty.clear();
      return;
    }
    stepLayout(snap, this.layoutState);
    // Copy mutated positions back into internal state.
    for (let i = 0; i < snap.ids.length; i++) {
      const slot = Number(snap.ids[i]);
      const node = this.nodes.get(slot);
      if (node) {
        node.x = snap.xs[i];
        node.y = snap.ys[i];
      }
    }
    this.dirty.clear();
  }

  /** Iterate node positions that changed since the last call.
   *  Used by the in-process client to write only what moved onto
   *  graphology, mirroring what the worker variant ships across
   *  postMessage. The callback fires for every diffed slot;
   *  `lastReported{X,Y}` is updated as we go so subsequent calls see
   *  only further changes. */
  forEachChangedPosition(
    cb: (slot: number, x: number, y: number) => void,
  ): void {
    for (const [slot, node] of this.nodes) {
      if (
        node.x === node.lastReportedX &&
        node.y === node.lastReportedY
      ) {
        continue;
      }
      cb(slot, node.x, node.y);
      node.lastReportedX = node.x;
      node.lastReportedY = node.y;
    }
  }

  /** Diff of positions changed since the last call, as parallel
   *  typed arrays. Returns `null` when no positions changed (the
   *  worker's tick loop skips the postMessage entirely in that
   *  case, saving 720KB+ of transfer per quiet tick at 30k nodes).
   *  `lastReported{X,Y}` is updated for every slot included in the
   *  returned diff. */
  snapshotChangedPositions(): {
    slots: Uint32Array;
    xs: Float64Array;
    ys: Float64Array;
  } | null {
    // Two-pass: count first to size the typed arrays exactly.
    let count = 0;
    for (const node of this.nodes.values()) {
      if (
        node.x !== node.lastReportedX ||
        node.y !== node.lastReportedY
      ) {
        count++;
      }
    }
    if (count === 0) return null;
    const slots = new Uint32Array(count);
    const xs = new Float64Array(count);
    const ys = new Float64Array(count);
    let i = 0;
    for (const [slot, node] of this.nodes) {
      if (
        node.x === node.lastReportedX &&
        node.y === node.lastReportedY
      ) {
        continue;
      }
      slots[i] = slot;
      xs[i] = node.x;
      ys[i] = node.y;
      node.lastReportedX = node.x;
      node.lastReportedY = node.y;
      i++;
    }
    return { slots, xs, ys };
  }

  // ---- Snapshot builder for stepLayout ----------------------------

  private buildSnapshot(): LayoutSnapshot | null {
    const N = this.nodes.size;
    if (N === 0) return null;

    // 1. Bucket slots by component root (component-major layout).
    const componentRoots: string[] = [];
    const seenRoot = new Set<string>();
    const rootBySlot = new Map<number, string>();
    for (const slot of this.nodes.keys()) {
      const root = findRoot(this.uf, String(slot));
      rootBySlot.set(slot, root);
      if (!seenRoot.has(root)) {
        seenRoot.add(root);
        componentRoots.push(root);
      }
    }
    const numComponents = componentRoots.length;

    const componentIdx = new Map<string, number>();
    for (let c = 0; c < numComponents; c++) {
      componentIdx.set(componentRoots[c], c);
    }

    // 2. Group slots by component, assigning component-major slot
    //    indices into the snapshot's typed arrays.
    const memberOffsets = new Int32Array(numComponents + 1);
    const members = new Int32Array(N);
    const slotInSnap = new Map<number, number>();
    const ids: string[] = new Array(N);
    const xs = new Float64Array(N);
    const ys = new Float64Array(N);
    const sizes = new Float64Array(N);
    const degrees = new Int32Array(N);
    const isTip = new Uint8Array(N);
    const community = new Int32Array(N);

    // Two-pass: count first, then fill.
    const memberCounts = new Int32Array(numComponents);
    for (const [, root] of rootBySlot) {
      memberCounts[componentIdx.get(root)!]++;
    }
    for (let c = 0; c < numComponents; c++) {
      memberOffsets[c + 1] = memberOffsets[c] + memberCounts[c];
    }
    const cursor = new Int32Array(numComponents);
    for (const [slot, node] of this.nodes) {
      const c = componentIdx.get(rootBySlot.get(slot)!)!;
      const idx = memberOffsets[c] + cursor[c];
      cursor[c]++;
      slotInSnap.set(slot, idx);
      ids[idx] = String(slot);
      xs[idx] = node.x;
      ys[idx] = node.y;
      sizes[idx] = Math.max(1, node.size);
      degrees[idx] = node.degree;
      isTip[idx] = node.isTip ? 1 : 0;
      community[idx] = node.community;
      members[idx] = idx;
    }

    // 3. Build per-component edges (deduped src<dst by snapshot index).
    const edgeOffsets = new Int32Array(numComponents + 1);
    // Upper bound on edges: sum of adj sizes / 2 (each edge counted
    // twice in adj). Allocate that.
    let edgeUpper = 0;
    for (const adj of this.adj.values()) edgeUpper += adj.size;
    edgeUpper = Math.ceil(edgeUpper / 2);
    const edgeSrc = new Int32Array(edgeUpper);
    const edgeDst = new Int32Array(edgeUpper);
    const edgeWeight = new Float32Array(edgeUpper);

    // Per-component edge cursors so we can write component-major.
    const edgePerComp: Array<Array<{ s: number; d: number; w: number }>> =
      Array.from({ length: numComponents }, () => []);

    for (const [src, adj] of this.adj) {
      const sIdx = slotInSnap.get(src);
      if (sIdx === undefined) continue;
      const c = componentIdx.get(rootBySlot.get(src)!)!;
      for (const [dst, w] of adj) {
        const dIdx = slotInSnap.get(dst);
        if (dIdx === undefined) continue;
        if (dIdx <= sIdx) continue; // dedup
        edgePerComp[c].push({ s: sIdx, d: dIdx, w });
      }
    }

    let eCursor = 0;
    for (let c = 0; c < numComponents; c++) {
      edgeOffsets[c] = eCursor;
      for (const e of edgePerComp[c]) {
        edgeSrc[eCursor] = e.s;
        edgeDst[eCursor] = e.d;
        edgeWeight[eCursor] = e.w;
        eCursor++;
      }
    }
    edgeOffsets[numComponents] = eCursor;

    // 4. Map dirty roots (string) to component indices (int).
    const dirtyArr: number[] = [];
    for (const root of this.dirty) {
      const c = componentIdx.get(root);
      if (c !== undefined) dirtyArr.push(c);
    }
    const dirtyComponents =
      dirtyArr.length > 0 ? new Int32Array(dirtyArr) : null;

    return {
      ids,
      xs,
      ys,
      sizes,
      degrees,
      isTip,
      community,
      numComponents,
      memberOffsets,
      members,
      edgeOffsets,
      edgeSrc: edgeSrc.subarray(0, eCursor),
      edgeDst: edgeDst.subarray(0, eCursor),
      edgeWeight: edgeWeight.subarray(0, eCursor),
      dirtyComponents,
    };
  }
}
