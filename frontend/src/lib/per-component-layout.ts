import type Graph from "graphology";
import type { ComponentState } from "@/lib/components";

/**
 * Per-component force simulation. Runs one step per animation frame
 * against each connected component's member set: pairwise repulsion
 * plus edge attraction, strictly within the component. No forces
 * cross component boundaries, which kills the cross-cluster drift we
 * saw with global FA2.
 *
 * Repulsion is O(N^2) per component, capped by a max-component-size
 * bail so a single giant hub doesn't stall the main thread. For
 * components above the cap we still do attraction (O(E)) but skip
 * pairwise repulsion  members stay roughly where edges drag them, and
 * the visual density remains acceptable because hub cores pack tight
 * anyway.
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
// separation. The previous cap meant a satellite needing 500 units of
// room would take 25+ frames to escape, and during that time the big
// cluster's own radius could grow and re-trap it.
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
// set of tips in each component (typically ≤8) so tips stay
// angularly distributed without piling up. Standard 1/d² with a
// large constant: only meaningful at short distances, harmless
// when tips are far apart.
const TIP_TIP_REPULSION = 80000;
// A node with this many visible edges is a megahub: probably a
// Jito tip account, DEX fee receiver, or other routing/aggregator
// wallet. We don't filter it (we want to see it exists), but its
// edges get a long rest length so its 100+ leaves spread out on a
// wide ring around it instead of compressing into a tight knot.
// The hub gets its own neighborhood within the connected component,
// other sub-clusters in the same component stay readable.
const MEGAHUB_VISIBLE_DEGREE = 50;
// Rest length for megahub edges. The leaf wants to sit this many
// world units from the hub, regardless of attraction strength.
// Tuned against the existing leaf-vs-hub equilibrium (~30 world
// units in normal star clusters) to give megahubs a roughly 8x
// wider footprint.
const MEGAHUB_EDGE_REST_LENGTH = 420;
// Components above this size are treated as "large": every edge in
// them gets a non-zero rest length so the searcher-to-searcher mesh
// stops packing tightly. Without this, leaves attached to multiple
// hubs or to other leaves collapse onto each other and the inner
// space between tips reads as a uniform jam.
const LARGE_COMPONENT_SIZE = 100;
const LARGE_COMPONENT_EDGE_REST_LENGTH = 90;
// Per-node velocity, keyed by node id. Survives across ticks so
// damping actually damps.
const velocities = new Map<string, { vx: number; vy: number }>();

export function resetLayoutVelocities(): void {
  velocities.clear();
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

export function stepPerComponentLayout(
  graph: Graph,
  components: ComponentState,
  nodeToCommunity?: Map<string, number>,
): void {
  for (const [, members] of components.members) {
    const n = members.size;
    if (n < MIN_ACTIVE_SIZE) continue;

    const ids = [...members];
    const xs = new Float64Array(n);
    const ys = new Float64Array(n);
    const sizes = new Float64Array(n);
    const fx = new Float64Array(n);
    const fy = new Float64Array(n);
    // Per-node community id, -1 if no community map is available yet
    // (first ~3s before Louvain runs). All -1 collapses to "everyone in
    // the same community" which means no cross-community boost, which
    // is the right fallback.
    const communities = new Int32Array(n);
    // Per-node degree, used to flag megahubs so their edges get a
    // long rest length and their leaves spread out.
    const degrees = new Int32Array(n);
    const idIndex = new Map<string, number>();
    // Indices of nodes in this component whose role is "tip-account".
    // Tips get extra forces below: tip-vs-non-tip repulsion (pushes
    // them outward against the searcher mass) and tip-vs-tip
    // repulsion (keeps them angularly distributed). No fixed angular
    // targets  positions emerge from force balance.
    const tipIndices: number[] = [];
    const isTip = new Uint8Array(n);

    for (let i = 0; i < n; i++) {
      const id = ids[i];
      idIndex.set(id, i);
      xs[i] = graph.getNodeAttribute(id, "x") as number;
      ys[i] = graph.getNodeAttribute(id, "y") as number;
      sizes[i] = Math.max(1, (graph.getNodeAttribute(id, "size") as number) ?? 1);
      communities[i] = nodeToCommunity?.get(id) ?? -1;
      degrees[i] = (graph.getNodeAttribute(id, "degree") as number) ?? 0;
      if (graph.getNodeAttribute(id, "role") === "tip-account") {
        tipIndices.push(i);
        isTip[i] = 1;
      }
    }

    // Pairwise repulsion, size-weighted so hubs claim their own space
    // and leaves orbit at an equilibrium proportional to the hub.
    // Cross-community pairs (within same connected component) get a
    // boost so visually-distinct Louvain sub-clusters peel apart even
    // when a bridging edge keeps them in the same component.
    // Skipped for giant components where O(N^2) would be too heavy.
    if (n <= MAX_N2_COMPONENT_SIZE) {
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const dx = xs[j] - xs[i];
          const dy = ys[j] - ys[i];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const sizeFactor = Math.pow(sizes[i] * sizes[j], SIZE_POW);
          const crossCommunity =
            communities[i] !== -1 &&
            communities[j] !== -1 &&
            communities[i] !== communities[j];
          const communityFactor = crossCommunity
            ? CROSS_COMMUNITY_REPULSION_FACTOR
            : 1;
          const f = (REPULSION * sizeFactor * communityFactor) / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[i] -= f * ux;
          fy[i] -= f * uy;
          fx[j] += f * ux;
          fy[j] += f * uy;
        }
      }
    }

    // Attraction over edges inside this component. We only iterate
    // edges incident on the first endpoint we encounter in each pair
    // (index ordering) to avoid double-counting.
    for (let i = 0; i < n; i++) {
      const srcId = ids[i];
      graph.forEachEdge(srcId, (_eid, attrs, source, target) => {
        const other = source === srcId ? target : source;
        const j = idIndex.get(other);
        if (j === undefined || j <= i) return;
        const dx = xs[j] - xs[i];
        const dy = ys[j] - ys[i];
        const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
        const weight = ((attrs.weight as number) ?? 1);
        // Megahub edges (either endpoint has 50+ counterparties) get
        // a non-zero rest length: they want distance d == REST, not
        // d == 0. If they're already inside the rest length, attraction
        // is zero and pairwise repulsion alone pushes them outward. If
        // they're stretched past it, normal attraction pulls them back.
        // Net effect: megahubs sit at the center of a wide leaf ring
        // instead of compressing all leaves into a tight knot.
        const isMegahubEdge =
          degrees[i] >= MEGAHUB_VISIBLE_DEGREE ||
          degrees[j] >= MEGAHUB_VISIBLE_DEGREE;
        const restLength = isMegahubEdge
          ? MEGAHUB_EDGE_REST_LENGTH
          : n >= LARGE_COMPONENT_SIZE
            ? LARGE_COMPONENT_EDGE_REST_LENGTH
            : 0;
        const stretch = d - restLength;
        if (stretch <= 0) return;
        const f = ATTRACTION * weight * stretch;
        const ux = dx / d;
        const uy = dy / d;
        fx[i] += f * ux;
        fy[i] += f * uy;
        fx[j] -= f * ux;
        fy[j] -= f * uy;
      });
    }

    // Tip-account positioning: organic, not ad-hoc. Two always-on
    // forces, no fixed angular targets or radii.
    //
    //  - Tip-vs-non-tip repulsion: every non-tip member of the
    //    component pushes each tip outward via standard 1/d². As the
    //    searcher mass grows, its outward pressure on tips grows
    //    with it, so tips drift to the cluster perimeter without us
    //    declaring a perimeter.
    //
    //  - Tip-vs-tip repulsion: small O(K^2) loop keeps tips
    //    angularly distributed without any of them piling on top of
    //    each other.
    //
    // Both run regardless of component size (the standard pairwise
    // loop above is gated by MAX_N2_COMPONENT_SIZE and skips for the
    // megacore). Equilibrium emerges from force balance with edge
    // attraction; nothing tells the tips where to be.
    if (tipIndices.length >= 1) {
      // Tip-vs-non-tip repulsion. O(tips * n) per component.
      // Reciprocal: also pushes the non-tip out, which keeps leaves
      // from packing too tightly against tip nodes.
      for (const i of tipIndices) {
        for (let j = 0; j < n; j++) {
          if (j === i || isTip[j]) continue;
          const dx = xs[j] - xs[i];
          const dy = ys[j] - ys[i];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const sizeFactor = Math.pow(sizes[i] * sizes[j], SIZE_POW);
          const f = (REPULSION * sizeFactor) / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[i] -= f * ux;
          fy[i] -= f * uy;
          fx[j] += f * ux;
          fy[j] += f * uy;
        }
      }

      // Tip-vs-tip repulsion.
      const numTips = tipIndices.length;
      for (let a = 0; a < numTips; a++) {
        const i = tipIndices[a];
        for (let b = a + 1; b < numTips; b++) {
          const j = tipIndices[b];
          const dx = xs[j] - xs[i];
          const dy = ys[j] - ys[i];
          const d2 = dx * dx + dy * dy + 0.01;
          const d = Math.sqrt(d2);
          const f = TIP_TIP_REPULSION / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[i] -= f * ux;
          fy[i] -= f * uy;
          fx[j] += f * ux;
          fy[j] += f * uy;
        }
      }
    }

    // Integrate with velocity + damping. Each node keeps a velocity
    // across frames; new force adds to it, damping bleeds it off. At
    // equilibrium, velocity asymptotes to zero and the node stops
    // moving  no more jitter.
    for (let i = 0; i < n; i++) {
      const id = ids[i];
      let v = velocities.get(id);
      if (!v) {
        v = { vx: 0, vy: 0 };
        velocities.set(id, v);
      }
      v.vx = v.vx * VELOCITY_DAMPING + fx[i] * STEP_SCALE;
      v.vy = v.vy * VELOCITY_DAMPING + fy[i] * STEP_SCALE;
      const speed = Math.hypot(v.vx, v.vy);
      if (speed > MAX_STEP) {
        const scale = MAX_STEP / speed;
        v.vx *= scale;
        v.vy *= scale;
      }
      xs[i] += v.vx;
      ys[i] += v.vy;
    }

    // Hard position-correction pass: any overlapping pair gets pushed
    // apart to exactly touchDistance. Not a force  a constraint, so
    // attraction and the MAX_STEP cap can't override it. Runs even on
    // huge components because we skip leaf-vs-leaf pairs (most of the
    // N^2 in a dense component is leaves).
    const hubIndices: number[] = [];
    for (let i = 0; i < n; i++) {
      if (sizes[i] >= COLLISION_HUB_SIZE) hubIndices.push(i);
    }
    for (let pass = 0; pass < 2; pass++) {
      // Hub vs everything: every hub checks against every node so it
      // can't have anything on top of it.
      for (const i of hubIndices) {
        for (let j = 0; j < n; j++) {
          if (j === i) continue;
          resolveOverlap(i, j, xs, ys, sizes);
        }
      }
    }

    // Flush final positions to the graph.
    for (let i = 0; i < n; i++) {
      graph.setNodeAttribute(ids[i], "x", xs[i]);
      graph.setNodeAttribute(ids[i], "y", ys[i]);
    }
  }

  pushComponentsApart(graph, components);
}

// Inter-component repulsion. Computes a centroid per component, then
// pushes nearby component centroids apart as rigid-body translations
// applied to every member. Keeps distinct clusters from stacking up
// in the same neighborhood even when their deterministic spawn
// positions happened to land close.
function pushComponentsApart(
  graph: Graph,
  components: ComponentState,
): void {
  interface Centroid {
    root: string;
    x: number;
    y: number;
    size: number;
    radius: number;
  }
  const centroids: Centroid[] = [];
  for (const [root, members] of components.members) {
    const size = members.size;
    if (size < MIN_COMPONENT_SIZE_FOR_PUSH) continue;
    let cx = 0;
    let cy = 0;
    for (const id of members) {
      cx += graph.getNodeAttribute(id, "x") as number;
      cy += graph.getNodeAttribute(id, "y") as number;
    }
    cx /= size;
    cy /= size;
    // Cluster radius is the boundary of the farthest member, not its
    // centerpoint. A hub with render size 10 extends 50 world units
    // (size * SIZE_TO_WORLD) past its center, so without this a
    // satellite can sit right against the visible edge of a fat hub
    // while we technically say there's "buffer" between centroids.
    let radius = 0;
    for (const id of members) {
      const x = graph.getNodeAttribute(id, "x") as number;
      const y = graph.getNodeAttribute(id, "y") as number;
      const nodeSize = (graph.getNodeAttribute(id, "size") as number) ?? 1;
      const d = Math.hypot(x - cx, y - cy) + nodeSize * SIZE_TO_WORLD;
      if (d > radius) radius = d;
    }
    centroids.push({ root, x: cx, y: cy, size, radius });
  }

  const translations = new Map<string, { dx: number; dy: number }>();
  for (let i = 0; i < centroids.length; i++) {
    for (let j = i + 1; j < centroids.length; j++) {
      const a = centroids[i];
      const b = centroids[j];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) + 0.0001;
      const required = a.radius + b.radius + COMPONENT_PUSH_BUFFER;
      if (d >= required) continue;
      // Resolve the full violation in one frame, not half. With size
      // splitting (shareA + shareB = 1), a push of (required - d)
      // distributes exactly the deficit across the two components, so
      // post-translation distance equals required. Halving it meant
      // the gap closed geometrically and never actually reached the
      // target while the bigger cluster's radius was still shifting.
      const push = required - d;
      const ux = dx / d;
      const uy = dy / d;
      // Split the translation by relative size: smaller component
      // yields more ground. Sticks big clusters in place.
      const total = a.size + b.size;
      const shareA = b.size / total;
      const shareB = a.size / total;
      const ta = translations.get(a.root) ?? { dx: 0, dy: 0 };
      const tb = translations.get(b.root) ?? { dx: 0, dy: 0 };
      ta.dx -= ux * push * shareA;
      ta.dy -= uy * push * shareA;
      tb.dx += ux * push * shareB;
      tb.dy += uy * push * shareB;
      translations.set(a.root, ta);
      translations.set(b.root, tb);
    }
  }

  // Apply translations directly. No cap: rigid component shifts don't
  // jitter, and uncapped resolution kills the slow-creep problem where
  // a satellite is still escaping while the big cluster expands around
  // it.
  for (const [root, t] of translations) {
    const members = components.members.get(root);
    if (!members) continue;
    for (const id of members) {
      graph.setNodeAttribute(id, "x", (graph.getNodeAttribute(id, "x") as number) + t.dx);
      graph.setNodeAttribute(id, "y", (graph.getNodeAttribute(id, "y") as number) + t.dy);
    }
  }
}
