/**
 * Per-component force layout, graphology-free.
 *
 * Pure module: takes a `LayoutSnapshot` of typed arrays describing the
 * graph, mutates `xs`/`ys` in place to step physics one frame, and
 * persists per-node velocities in a caller-owned `LayoutState`. The
 * caller (today: `use-raw-stream.ts`) materializes the snapshot from
 * its graphology instance once per frame; tomorrow this same module
 * runs inside a Web Worker and the snapshot comes from a transferred
 * `Float32Array` instead.
 *
 * Forces per step (matches the prior in-place implementation
 * bit-for-bit, only the data source changed):
 *   1. Pairwise repulsion (gated by MAX_N2_COMPONENT_SIZE).
 *   2. Edge attraction with megahub / large-component rest lengths.
 *   3. Tip-vs-non-tip and tip-vs-tip repulsion.
 *   4. Velocity integration with damping.
 *   5. Hub collision pass (hard constraint).
 * Then `pushComponentsApart` runs over all components as rigid
 * centroid-based translations.
 *
 * No graphology imports. No DOM access. Safe to run in a worker.
 */

const STEP_SCALE = 0.35;
// Tuned so a degree-5 leaf sits ~40 units from its hub  far enough
// that the cluster has visible radial structure, close enough that
// the edge reads as a connection not a bridge.
const REPULSION = 300;
const ATTRACTION = 0.002;
// Size factor exponent: repulsion scales as (sizeA * sizeB)^SIZE_POW.
// >0 makes bigger nodes repel harder, which creates natural hierarchy
// (big hubs keep their personal space, small nodes orbit them).
const SIZE_POW = 0.9;
// For components above this size, pairwise repulsion is too expensive
// and we skip it  attraction alone keeps the structure together, and
// the collision pass below still runs because it's filtered to "big"
// nodes only.
const MAX_N2_COMPONENT_SIZE = 400;
const MIN_ACTIVE_SIZE = 2;
// Nodes at or above this size are treated as hubs for collision. We
// always check hub-vs-anything pairs but skip leaf-vs-leaf  leaves
// are small enough that the occasional overlap is invisible, and
// skipping L*L pairs cuts the big-component cost from O(N^2) to
// O(H*N) where H is the small number of hubs.
const COLLISION_HUB_SIZE = 3;
// Size values come from nodeSize() tuned for Sigma pixel rendering
// (0.8 to 10). Our layout is in world units where cluster extent
// spans 100+. Multiply pixel size by SIZE_TO_WORLD to get the "personal
// space" radius a node claims  otherwise collision radius of ~20
// world units is invisible against 100+ cluster extents, and hubs
// appear stacked at zoom.
const SIZE_TO_WORLD = 5;
// Extra distance kept between circle edges even when attraction wants
// them touching. Reads as breathing room in the layout.
const COLLISION_MARGIN = 8;
// Velocity damping per frame. Without this, tiny residual forces at
// equilibrium keep jittering positions every frame; 4000 nodes each
// moving fractions of a pixel reads as constant visible motion. 0.7
// is fast enough to still respond to new edges, stable enough to not
// hum.
const VELOCITY_DAMPING = 0.7;
// Hard cap on how far a node can move per frame. Prevents a rare
// huge-force event (new edge between distant components, e.g.) from
// flinging a node across the canvas.
const MAX_STEP = 30;
// Inter-component repulsion. Required gap between two component
// centroids = radiusA + radiusB + buffer, so a big spread-out cluster
// actually makes room for its radius rather than letting a small
// cluster sit inside its outer ring. No per-frame cap: component
// translations are rigid (every member moves by the same delta), so
// snapping them apart in one frame produces no jitter, only a clean
// separation.
const COMPONENT_PUSH_BUFFER = 400;
const MIN_COMPONENT_SIZE_FOR_PUSH = 2;
// Within a single connected component, two nodes that belong to
// different Louvain communities feel boosted repulsion. This pulls
// visually-distinct sub-clusters apart even when a bridging edge
// keeps them in the same Union-Find component. The bridging edge's
// attraction still holds them connected; it just stretches longer,
// which is the desired visual ("you can see the bridge, but the
// communities don't sit on top of each other").
const CROSS_COMMUNITY_REPULSION_FACTOR = 20;
// Tip-vs-tip repulsion. Always-active O(K^2) loop over the small
// set of tips in each component (typically <=8) so tips stay
// angularly distributed without piling up.
const TIP_TIP_REPULSION = 80000;
// A node with this many visible edges is a megahub: probably a
// Jito tip account, DEX fee receiver, or other routing/aggregator
// wallet. Its edges get a long rest length so its 100+ leaves
// spread out on a wide ring around it instead of compressing into
// a tight knot.
const MEGAHUB_VISIBLE_DEGREE = 50;
const MEGAHUB_EDGE_REST_LENGTH = 420;
// Components above this size are treated as "large": every edge in
// them gets a non-zero rest length so the searcher-to-searcher mesh
// stops packing tightly.
const LARGE_COMPONENT_SIZE = 100;
const LARGE_COMPONENT_EDGE_REST_LENGTH = 90;

/**
 * Per-frame description of the graph in typed-array form. The caller
 * materializes this from whatever its source of truth is (graphology
 * today, a transferred buffer in the worker). Once built, every field
 * is positionally indexed by a "slot" `0..N-1`. Slots are ordered
 * component-major so members of one component occupy a contiguous
 * range described by `memberOffsets`.
 *
 * `xs` and `ys` are mutated in place by `stepLayout`; everything else
 * is read-only this tick.
 */
export interface LayoutSnapshot {
  /** Stable node ids in slot order. Used to key `LayoutState.velocities`. */
  ids: string[];
  xs: Float64Array;
  ys: Float64Array;
  sizes: Float64Array;
  degrees: Int32Array;
  /** 1 iff role === "tip-account". */
  isTip: Uint8Array;
  /** Per-node Louvain community id, -1 if unknown. `null` disables the
   *  cross-community repulsion boost (first ~3s before Louvain runs). */
  community: Int32Array | null;

  /** Component grouping, CSR-style.
   *  Members of component `c` are `members[memberOffsets[c]..memberOffsets[c+1])`,
   *  each entry being a slot index. */
  numComponents: number;
  memberOffsets: Int32Array;
  members: Int32Array;

  /** Intra-component edges, CSR-style. Each edge is listed once,
   *  with `edgeSrc[k] < edgeDst[k]` (slot order). */
  edgeOffsets: Int32Array;
  edgeSrc: Int32Array;
  edgeDst: Int32Array;
  edgeWeight: Float32Array;

  /** Components that received a new edge this frame. `null` means
   *  step every component (used for first paint or non-streaming
   *  callers). `pushComponentsApart` always runs over every component
   *  regardless because a dirty cluster's growth can collide with a
   *  stationary neighbor. */
  dirtyComponents: Int32Array | null;
}

/**
 * Persistent state across frames. Owned by the caller so the worker
 * version can hold its own copy without crossing the postMessage
 * boundary. `velocities` damps motion at equilibrium; `fx`/`fy` are
 * scratch force buffers reused across frames to avoid per-frame
 * allocation pressure (480KB at 30k nodes).
 */
export interface LayoutState {
  velocities: Map<string, { vx: number; vy: number }>;
  fx: Float64Array;
  fy: Float64Array;
}

export function createLayoutState(): LayoutState {
  return {
    velocities: new Map(),
    fx: new Float64Array(0),
    fy: new Float64Array(0),
  };
}

export function resetLayoutState(state: LayoutState): void {
  state.velocities.clear();
}

/** Keep a stable Int32Array of `[0, 1, ..., k-1]` for the
 *  no-dirty-set fallback path. Reused across frames. */
let _allComponentsScratch: Int32Array = new Int32Array(0);
function allComponents(k: number): Int32Array {
  if (_allComponentsScratch.length < k) {
    _allComponentsScratch = new Int32Array(k);
    for (let i = 0; i < k; i++) _allComponentsScratch[i] = i;
  } else {
    // Already filled with [0,1,...,len-1] from prior call; reuse the
    // first k entries as long as the prefix is still correct.
    if (_allComponentsScratch[k - 1] !== k - 1) {
      for (let i = 0; i < k; i++) _allComponentsScratch[i] = i;
    }
  }
  return _allComponentsScratch.subarray(0, k);
}

/**
 * Step physics one frame. Mutates `snap.xs`, `snap.ys`, and
 * `state.velocities` in place. Force arrays in `state` are zeroed
 * and reused.
 */
export function stepLayout(snap: LayoutSnapshot, state: LayoutState): void {
  const N = snap.ids.length;
  if (state.fx.length < N) {
    state.fx = new Float64Array(N);
    state.fy = new Float64Array(N);
  }
  state.fx.fill(0, 0, N);
  state.fy.fill(0, 0, N);
  const fx = state.fx;
  const fy = state.fy;

  const xs = snap.xs;
  const ys = snap.ys;
  const sizes = snap.sizes;
  const degrees = snap.degrees;
  const isTip = snap.isTip;
  const community = snap.community;

  const components = snap.dirtyComponents ?? allComponents(snap.numComponents);

  for (let dIdx = 0; dIdx < components.length; dIdx++) {
    const c = components[dIdx];
    const memStart = snap.memberOffsets[c];
    const memEnd = snap.memberOffsets[c + 1];
    const n = memEnd - memStart;
    if (n < MIN_ACTIVE_SIZE) continue;

    // (1) Pairwise repulsion within the component.
    if (n <= MAX_N2_COMPONENT_SIZE) {
      for (let ii = 0; ii < n; ii++) {
        const si = snap.members[memStart + ii];
        for (let jj = ii + 1; jj < n; jj++) {
          const sj = snap.members[memStart + jj];
          const dx = xs[sj] - xs[si];
          const dy = ys[sj] - ys[si];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const sizeFactor = Math.pow(sizes[si] * sizes[sj], SIZE_POW);
          const cI = community ? community[si] : -1;
          const cJ = community ? community[sj] : -1;
          const crossCommunity = cI !== -1 && cJ !== -1 && cI !== cJ;
          const communityFactor = crossCommunity
            ? CROSS_COMMUNITY_REPULSION_FACTOR
            : 1;
          const f = (REPULSION * sizeFactor * communityFactor) / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[si] -= f * ux;
          fy[si] -= f * uy;
          fx[sj] += f * ux;
          fy[sj] += f * uy;
        }
      }
    }

    // (2) Attraction along intra-component edges.
    const eStart = snap.edgeOffsets[c];
    const eEnd = snap.edgeOffsets[c + 1];
    for (let k = eStart; k < eEnd; k++) {
      const si = snap.edgeSrc[k];
      const sj = snap.edgeDst[k];
      const dx = xs[sj] - xs[si];
      const dy = ys[sj] - ys[si];
      const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const w = snap.edgeWeight[k];
      const isMegahubEdge =
        degrees[si] >= MEGAHUB_VISIBLE_DEGREE ||
        degrees[sj] >= MEGAHUB_VISIBLE_DEGREE;
      const restLength = isMegahubEdge
        ? MEGAHUB_EDGE_REST_LENGTH
        : n >= LARGE_COMPONENT_SIZE
          ? LARGE_COMPONENT_EDGE_REST_LENGTH
          : 0;
      const stretch = d - restLength;
      if (stretch <= 0) continue;
      const f = ATTRACTION * w * stretch;
      const ux = dx / d;
      const uy = dy / d;
      fx[si] += f * ux;
      fy[si] += f * uy;
      fx[sj] -= f * ux;
      fy[sj] -= f * uy;
    }

    // (3) Tip forces. Collect tip slots within this component, then
    // run tip-vs-non-tip and tip-vs-tip in O(K*n + K^2). K is
    // typically <=8 so both loops are cheap regardless of n.
    const tipSlots: number[] = [];
    for (let ii = 0; ii < n; ii++) {
      const s = snap.members[memStart + ii];
      if (isTip[s]) tipSlots.push(s);
    }
    if (tipSlots.length >= 1) {
      for (const si of tipSlots) {
        for (let ii = 0; ii < n; ii++) {
          const sj = snap.members[memStart + ii];
          if (sj === si || isTip[sj]) continue;
          const dx = xs[sj] - xs[si];
          const dy = ys[sj] - ys[si];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const sizeFactor = Math.pow(sizes[si] * sizes[sj], SIZE_POW);
          const f = (REPULSION * sizeFactor) / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[si] -= f * ux;
          fy[si] -= f * uy;
          fx[sj] += f * ux;
          fy[sj] += f * uy;
        }
      }
      const numTips = tipSlots.length;
      for (let a = 0; a < numTips; a++) {
        const si = tipSlots[a];
        for (let b = a + 1; b < numTips; b++) {
          const sj = tipSlots[b];
          const dx = xs[sj] - xs[si];
          const dy = ys[sj] - ys[si];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const f = TIP_TIP_REPULSION / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[si] -= f * ux;
          fy[si] -= f * uy;
          fx[sj] += f * ux;
          fy[sj] += f * uy;
        }
      }
    }

    // (4) Velocity integration. Each node keeps a velocity across
    // frames; new force adds, damping bleeds off. At equilibrium,
    // velocity asymptotes to zero and the node stops moving.
    for (let ii = 0; ii < n; ii++) {
      const s = snap.members[memStart + ii];
      const id = snap.ids[s];
      let v = state.velocities.get(id);
      if (!v) {
        v = { vx: 0, vy: 0 };
        state.velocities.set(id, v);
      }
      v.vx = v.vx * VELOCITY_DAMPING + fx[s] * STEP_SCALE;
      v.vy = v.vy * VELOCITY_DAMPING + fy[s] * STEP_SCALE;
      const speed = Math.hypot(v.vx, v.vy);
      if (speed > MAX_STEP) {
        const sc = MAX_STEP / speed;
        v.vx *= sc;
        v.vy *= sc;
      }
      xs[s] += v.vx;
      ys[s] += v.vy;
    }

    // (5) Hub collision. A constraint, not a force: hubs check
    // against everything in the component and push out by exactly
    // the overlap. Two passes converge tighter than one.
    const hubSlots: number[] = [];
    for (let ii = 0; ii < n; ii++) {
      const s = snap.members[memStart + ii];
      if (sizes[s] >= COLLISION_HUB_SIZE) hubSlots.push(s);
    }
    for (let pass = 0; pass < 2; pass++) {
      for (const si of hubSlots) {
        for (let ii = 0; ii < n; ii++) {
          const sj = snap.members[memStart + ii];
          if (sj === si) continue;
          resolveOverlap(si, sj, xs, ys, sizes);
        }
      }
    }
  }

  pushComponentsApart(snap);
}

function resolveOverlap(
  i: number,
  j: number,
  xs: Float64Array,
  ys: Float64Array,
  sizes: Float64Array,
): void {
  const dx = xs[j] - xs[i];
  const dy = ys[j] - ys[i];
  const d2 = dx * dx + dy * dy + 0.0001;
  const d = Math.sqrt(d2);
  const touchDistance =
    (sizes[i] + sizes[j]) * SIZE_TO_WORLD + COLLISION_MARGIN;
  if (d >= touchDistance) return;
  const overlap = touchDistance - d;
  const totalSize = sizes[i] + sizes[j];
  const shareI = sizes[j] / totalSize;
  const shareJ = sizes[i] / totalSize;
  const ux = dx / d;
  const uy = dy / d;
  xs[i] -= ux * overlap * shareI;
  ys[i] -= uy * overlap * shareI;
  xs[j] += ux * overlap * shareJ;
  ys[j] += uy * overlap * shareJ;
}

/**
 * Inter-component repulsion. Computes a centroid + radius per
 * component, then resolves any overlap as a rigid translation
 * applied to every member of the affected component(s). Always
 * iterates every component (not just dirty) because a dirty
 * cluster's growth can push a stationary neighbor away.
 */
function pushComponentsApart(snap: LayoutSnapshot): void {
  const numComponents = snap.numComponents;
  // Per-component centroid + radius. Allocated fresh each step;
  // numComponents is small (thousands) so this is cheap.
  const cx = new Float64Array(numComponents);
  const cy = new Float64Array(numComponents);
  const radius = new Float64Array(numComponents);
  const compSize = new Int32Array(numComponents);
  // 0 means "skipped" (size below MIN_COMPONENT_SIZE_FOR_PUSH); we
  // mark these with sentinel size = 0 so the pair loop skips them.

  for (let c = 0; c < numComponents; c++) {
    const memStart = snap.memberOffsets[c];
    const memEnd = snap.memberOffsets[c + 1];
    const n = memEnd - memStart;
    if (n < MIN_COMPONENT_SIZE_FOR_PUSH) continue;
    compSize[c] = n;
    let sx = 0;
    let sy = 0;
    for (let ii = 0; ii < n; ii++) {
      const s = snap.members[memStart + ii];
      sx += snap.xs[s];
      sy += snap.ys[s];
    }
    cx[c] = sx / n;
    cy[c] = sy / n;
    let r = 0;
    for (let ii = 0; ii < n; ii++) {
      const s = snap.members[memStart + ii];
      const x = snap.xs[s];
      const y = snap.ys[s];
      const sz = snap.sizes[s];
      const d = Math.hypot(x - cx[c], y - cy[c]) + sz * SIZE_TO_WORLD;
      if (d > r) r = d;
    }
    radius[c] = r;
  }

  // Translation accumulator per component.
  const tx = new Float64Array(numComponents);
  const ty = new Float64Array(numComponents);

  for (let a = 0; a < numComponents; a++) {
    if (compSize[a] === 0) continue;
    for (let b = a + 1; b < numComponents; b++) {
      if (compSize[b] === 0) continue;
      const dx = cx[b] - cx[a];
      const dy = cy[b] - cy[a];
      const d = Math.hypot(dx, dy) + 0.0001;
      const required = radius[a] + radius[b] + COMPONENT_PUSH_BUFFER;
      if (d >= required) continue;
      // Resolve in one frame (rigid translation, no jitter).
      const push = required - d;
      const ux = dx / d;
      const uy = dy / d;
      const total = compSize[a] + compSize[b];
      const shareA = compSize[b] / total;
      const shareB = compSize[a] / total;
      tx[a] -= ux * push * shareA;
      ty[a] -= uy * push * shareA;
      tx[b] += ux * push * shareB;
      ty[b] += uy * push * shareB;
    }
  }

  for (let c = 0; c < numComponents; c++) {
    if (tx[c] === 0 && ty[c] === 0) continue;
    const memStart = snap.memberOffsets[c];
    const memEnd = snap.memberOffsets[c + 1];
    const dxC = tx[c];
    const dyC = ty[c];
    for (let ii = memStart; ii < memEnd; ii++) {
      const s = snap.members[ii];
      snap.xs[s] += dxC;
      snap.ys[s] += dyC;
    }
  }
}
