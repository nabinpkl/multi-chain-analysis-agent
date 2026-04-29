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
// Components below this size use the naive O(N^2) repulsion loop;
// build/walk overhead of the quadtree exceeds direct pair work for
// small N. Above this, Barnes-Hut wins and there is no upper bound
// (we used to silently disable repulsion past 400 nodes, which is
// what made mega-components knot up).
const SMALL_COMPONENT_THRESHOLD = 64;
// Barnes-Hut acceptance criterion. A tree node is treated as a
// single point if (boxWidth / distance) < theta. Stored squared so
// the hot test is `w*w < theta*theta * d2` with no sqrt. 0.9 is
// aggressive but layout is approximate by construction (damping +
// MAX_STEP); cluster shape stays the same vs theta=0.7 and the walk
// is ~2x faster.
const BARNES_HUT_THETA_SQ = 0.81;
// Cap quadtree depth so coincident points (rare with hashed spawn,
// possible after merges) bottom out as a "bucket" leaf instead of
// looping forever. At depth 24 the cell is ~6e-7 of the original
// bbox  far below the +0.01 force epsilon, so treating the bucket
// as one point at its COM is physically equivalent to per-pair
// computation.
const TREE_MAX_DEPTH = 24;
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

// ---------------------------------------------------------------
// Barnes-Hut quadtree (module-scoped scratch).
//
// One tree is built per component per frame, then queried by every
// member of that component to accumulate O(N log N) far-field
// repulsion. All state lives in typed-array pools that grow
// monotonically across frames; per-frame work is just zeroing a
// node-count cursor. No allocations on the hot path.
//
// Per-node storage (one entry per tree node, indexed 0.._treeNodeCount):
//   _treeMass[i]     during build: running mass sum; after finalize: same
//   _treeMx[i]       during build: sum of x*mass; after finalize: COM x
//   _treeMy[i]       during build: sum of y*mass; after finalize: COM y
//   _treeCx[i]       bbox center x (subdivision frame, not COM)
//   _treeCy[i]       bbox center y
//   _treeW[i]        bbox width (max of dx, dy of the cell)
//   _treeChildren[i*4 + k]  child node index 0..3 (NW/NE/SW/SE), -1 if absent
//   _treeLeafSlot[i] >= 0: leaf with one point, that slot's id.
//                    -1: internal (children populated).
//                    -2: bucket leaf (>=2 coincident points; no children).
//   _treeCount[i]    number of points in this subtree
//   _treeCommunity[i] -2 unset, -1 mixed, else dominant community id
//
// `_masses` is per-snapshot-slot (length N, indexed by snap slot id),
// holding sizes[s]^SIZE_POW so the quadtree can aggregate mass
// without recomputing pow inside the hot loop.
// ---------------------------------------------------------------

let _masses: Float64Array = new Float64Array(0);

let _treeCapacity = 0;
let _treeMass: Float64Array = new Float64Array(0);
let _treeMx: Float64Array = new Float64Array(0);
let _treeMy: Float64Array = new Float64Array(0);
let _treeCx: Float64Array = new Float64Array(0);
let _treeCy: Float64Array = new Float64Array(0);
let _treeW: Float64Array = new Float64Array(0);
let _treeChildren: Int32Array = new Int32Array(0);
let _treeLeafSlot: Int32Array = new Int32Array(0);
let _treeCount: Int32Array = new Int32Array(0);
let _treeCommunity: Int32Array = new Int32Array(0);
let _treeNodeCount = 0;

// Worst-case walk depth at any point is TREE_MAX_DEPTH * 4 (push
// all four children at each level). Pre-allocated; never grows.
const _traversalStack = new Int32Array(TREE_MAX_DEPTH * 4 + 16);

function ensureTreeCapacity(target: number): void {
  if (target <= _treeCapacity) return;
  const cap = Math.max(_treeCapacity * 2, target, 256);
  const newMass = new Float64Array(cap);
  newMass.set(_treeMass);
  _treeMass = newMass;
  const newMx = new Float64Array(cap);
  newMx.set(_treeMx);
  _treeMx = newMx;
  const newMy = new Float64Array(cap);
  newMy.set(_treeMy);
  _treeMy = newMy;
  const newCx = new Float64Array(cap);
  newCx.set(_treeCx);
  _treeCx = newCx;
  const newCy = new Float64Array(cap);
  newCy.set(_treeCy);
  _treeCy = newCy;
  const newW = new Float64Array(cap);
  newW.set(_treeW);
  _treeW = newW;
  const newChildren = new Int32Array(cap * 4);
  newChildren.set(_treeChildren);
  _treeChildren = newChildren;
  const newLeaf = new Int32Array(cap);
  newLeaf.set(_treeLeafSlot);
  _treeLeafSlot = newLeaf;
  const newCount = new Int32Array(cap);
  newCount.set(_treeCount);
  _treeCount = newCount;
  const newCommunity = new Int32Array(cap);
  newCommunity.set(_treeCommunity);
  _treeCommunity = newCommunity;
  _treeCapacity = cap;
}

function newTreeNode(cx: number, cy: number, w: number): number {
  const i = _treeNodeCount++;
  _treeMass[i] = 0;
  _treeMx[i] = 0;
  _treeMy[i] = 0;
  _treeCx[i] = cx;
  _treeCy[i] = cy;
  _treeW[i] = w;
  const c4 = i * 4;
  _treeChildren[c4] = -1;
  _treeChildren[c4 + 1] = -1;
  _treeChildren[c4 + 2] = -1;
  _treeChildren[c4 + 3] = -1;
  _treeLeafSlot[i] = -1;
  _treeCount[i] = 0;
  _treeCommunity[i] = -2;
  return i;
}

function newChildOf(parentIdx: number, k: number): number {
  const half = _treeW[parentIdx] * 0.5;
  const quarter = half * 0.5;
  const px = _treeCx[parentIdx];
  const py = _treeCy[parentIdx];
  const cx = px + ((k & 1) ? quarter : -quarter);
  const cy = py + ((k & 2) ? quarter : -quarter);
  const idx = newTreeNode(cx, cy, half);
  _treeChildren[parentIdx * 4 + k] = idx;
  return idx;
}

function insertIntoTree(
  rootIdx: number,
  slot: number,
  x: number,
  y: number,
  m: number,
  community: number,
  xs: Float64Array,
  ys: Float64Array,
  masses: Float64Array,
  communityArr: Int32Array | null,
): void {
  let idx = rootIdx;
  let depth = 0;
  // Iterative descent. Each iteration: update aggregates at idx,
  // decide whether to land here (empty leaf) or descend.
  for (;;) {
    _treeMass[idx] += m;
    _treeMx[idx] += x * m;
    _treeMy[idx] += y * m;
    _treeCount[idx] += 1;
    {
      const cur = _treeCommunity[idx];
      if (cur === -2) {
        _treeCommunity[idx] = community;
      } else if (cur !== -1 && cur !== community) {
        // Differ (including either side being -1 unknown). Treat as
        // mixed so the boost is conservatively skipped at this node.
        _treeCommunity[idx] = -1;
      }
    }

    if (_treeCount[idx] === 1) {
      // First point in this subtree: park here as a single leaf.
      _treeLeafSlot[idx] = slot;
      return;
    }

    // Subtree now has >= 2 points. If we're still a single leaf,
    // split (or, at max depth, become a bucket).
    if (_treeLeafSlot[idx] >= 0) {
      if (depth >= TREE_MAX_DEPTH) {
        _treeLeafSlot[idx] = -2; // bucket marker; aggregates already include both points
        return;
      }
      const priorSlot = _treeLeafSlot[idx];
      _treeLeafSlot[idx] = -1;
      const priorX = xs[priorSlot];
      const priorY = ys[priorSlot];
      const priorM = masses[priorSlot];
      const priorC = communityArr ? communityArr[priorSlot] : -1;
      const pK =
        (priorX >= _treeCx[idx] ? 1 : 0) | (priorY >= _treeCy[idx] ? 2 : 0);
      // childK 0..3, may or may not collide with the new point's quadrant.
      let priorChildIdx = _treeChildren[idx * 4 + pK];
      if (priorChildIdx === -1) priorChildIdx = newChildOf(idx, pK);
      // Seed the child with the prior point's contribution. If the
      // new point hits the same quadrant, the next iteration will
      // add its contribution on top via the standard aggregate
      // update at the loop head.
      _treeMass[priorChildIdx] = priorM;
      _treeMx[priorChildIdx] = priorX * priorM;
      _treeMy[priorChildIdx] = priorY * priorM;
      _treeCount[priorChildIdx] = 1;
      _treeCommunity[priorChildIdx] = priorC;
      _treeLeafSlot[priorChildIdx] = priorSlot;
    } else if (_treeLeafSlot[idx] === -2) {
      // Already a bucket; aggregates are correct, nothing to do.
      return;
    }

    // Descend into the new point's quadrant.
    const k = (x >= _treeCx[idx] ? 1 : 0) | (y >= _treeCy[idx] ? 2 : 0);
    let childIdx = _treeChildren[idx * 4 + k];
    if (childIdx === -1) childIdx = newChildOf(idx, k);
    idx = childIdx;
    depth += 1;
  }
}

function buildQuadtree(
  snap: LayoutSnapshot,
  memStart: number,
  n: number,
): number {
  // Bbox of this component's members.
  const xs = snap.xs;
  const ys = snap.ys;
  let xmin = Infinity;
  let ymin = Infinity;
  let xmax = -Infinity;
  let ymax = -Infinity;
  for (let ii = 0; ii < n; ii++) {
    const s = snap.members[memStart + ii];
    const x = xs[s];
    const y = ys[s];
    if (x < xmin) xmin = x;
    if (x > xmax) xmax = x;
    if (y < ymin) ymin = y;
    if (y > ymax) ymax = y;
  }
  const cx = (xmin + xmax) * 0.5;
  const cy = (ymin + ymax) * 0.5;
  let w = Math.max(xmax - xmin, ymax - ymin);
  // Guard the all-coincident case so subdivision still has a frame.
  if (w < 1) w = 1;
  // Tiny epsilon so points exactly on the right/bottom edge land
  // inside the cell after the >= comparison in `quadrant`.
  w *= 1.0001;

  // Reserve enough nodes that no insert hits a realloc. Worst case
  // for a fully unique distribution is ~4N/3 nodes; over-provision
  // to cover splits at any depth.
  ensureTreeCapacity(_treeNodeCount + 4 * n + 8);
  _treeNodeCount = 0;
  const rootIdx = newTreeNode(cx, cy, w);

  const masses = _masses;
  const community = snap.community;

  for (let ii = 0; ii < n; ii++) {
    const s = snap.members[memStart + ii];
    const x = xs[s];
    const y = ys[s];
    const m = masses[s];
    const c = community ? community[s] : -1;
    insertIntoTree(rootIdx, s, x, y, m, c, xs, ys, masses, community);
  }

  // Convert running (sum of x*mass) / (sum of y*mass) to COM by
  // dividing by total mass. After this pass _treeMx / _treeMy hold
  // the center of mass per node.
  for (let i = 0; i < _treeNodeCount; i++) {
    const m = _treeMass[i];
    if (m > 0) {
      _treeMx[i] /= m;
      _treeMy[i] /= m;
    }
  }

  return rootIdx;
}

function accumulateRepulsion(
  siSlot: number,
  rootIdx: number,
  xs: Float64Array,
  ys: Float64Array,
  sizes: Float64Array,
  fx: Float64Array,
  fy: Float64Array,
  community: Int32Array | null,
): void {
  const sx = xs[siSlot];
  const sy = ys[siSlot];
  const sMass = _masses[siSlot];
  const sCommunity = community ? community[siSlot] : -1;
  const stack = _traversalStack;
  let top = 0;
  stack[top++] = rootIdx;
  while (top > 0) {
    const idx = stack[--top];
    if (_treeCount[idx] === 0) continue;

    const dx = _treeMx[idx] - sx;
    const dy = _treeMy[idx] - sy;
    const d2 = dx * dx + dy * dy + 0.01;
    const w = _treeW[idx];

    const leafSlot = _treeLeafSlot[idx];
    if (leafSlot >= 0) {
      // Single-point leaf.
      if (leafSlot === siSlot) continue;
      const sj = leafSlot;
      const d = Math.sqrt(d2);
      const sizeFactor = Math.pow(sizes[siSlot] * sizes[sj], SIZE_POW);
      const cI = sCommunity;
      const cJ = community ? community[sj] : -1;
      const crossCommunity = cI !== -1 && cJ !== -1 && cI !== cJ;
      const cf = crossCommunity ? CROSS_COMMUNITY_REPULSION_FACTOR : 1;
      const f = (REPULSION * sizeFactor * cf) / d2;
      const ux = dx / d;
      const uy = dy / d;
      fx[siSlot] -= f * ux;
      fy[siSlot] -= f * uy;
      continue;
    }

    if (leafSlot === -2 || w * w < BARNES_HUT_THETA_SQ * d2) {
      // Far enough (or bucket of coincident points): treat as one
      // point at COM with summed mass. Force on si only; the
      // represented points each walk the tree on their own.
      const M = _treeMass[idx];
      const cI = sCommunity;
      const cJ = _treeCommunity[idx];
      const crossCommunity = cI !== -1 && cJ !== -1 && cI !== cJ;
      const cf = crossCommunity ? CROSS_COMMUNITY_REPULSION_FACTOR : 1;
      const f = (REPULSION * sMass * M * cf) / d2;
      const d = Math.sqrt(d2);
      const ux = dx / d;
      const uy = dy / d;
      fx[siSlot] -= f * ux;
      fy[siSlot] -= f * uy;
      continue;
    }

    // Internal, too close: recurse.
    const c4 = idx * 4;
    const c0 = _treeChildren[c4];
    const c1 = _treeChildren[c4 + 1];
    const c2 = _treeChildren[c4 + 2];
    const c3 = _treeChildren[c4 + 3];
    if (c0 !== -1) stack[top++] = c0;
    if (c1 !== -1) stack[top++] = c1;
    if (c2 !== -1) stack[top++] = c2;
    if (c3 !== -1) stack[top++] = c3;
  }
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

  // Pre-compute per-slot effective mass (size^SIZE_POW) for the
  // Barnes-Hut tree. The pair force is REPULSION * (sizeI*sizeJ)^p,
  // and (a*b)^p == a^p * b^p, so a tree node's summed mass times the
  // query node's mass gives the correct aggregate force without
  // per-pair pow().
  if (_masses.length < N) {
    _masses = new Float64Array(N);
  }
  for (let s = 0; s < N; s++) {
    _masses[s] = Math.pow(sizes[s], SIZE_POW);
  }

  const components = snap.dirtyComponents ?? allComponents(snap.numComponents);

  for (let dIdx = 0; dIdx < components.length; dIdx++) {
    const c = components[dIdx];
    const memStart = snap.memberOffsets[c];
    const memEnd = snap.memberOffsets[c + 1];
    const n = memEnd - memStart;
    if (n < MIN_ACTIVE_SIZE) continue;

    // (1) Pairwise repulsion within the component.
    //
    // Two paths: at small N the naive O(N^2) double loop is the
    // cheapest option (no tree build, tight inner loop); above the
    // threshold a Barnes-Hut quadtree drops the cost to O(N log N)
    // per query and lets components of any size feel real
    // repulsion. The previous implementation silently disabled
    // repulsion past 400 nodes, which is what made mega-hubs look
    // knotted; that gate is gone.
    if (n < SMALL_COMPONENT_THRESHOLD) {
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
    } else {
      const rootIdx = buildQuadtree(snap, memStart, n);
      for (let ii = 0; ii < n; ii++) {
        const si = snap.members[memStart + ii];
        accumulateRepulsion(
          si,
          rootIdx,
          xs,
          ys,
          sizes,
          fx,
          fy,
          community,
        );
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
