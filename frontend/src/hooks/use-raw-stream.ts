"use client";

import Graph from "graphology";
import { useEffect, useRef, useState } from "react";
import { subscribeRawStream, type RawEdge } from "@/lib/api";
import {
  addNode as addToComponent,
  createComponentState,
  findRoot,
  union,
  type ComponentState,
} from "@/lib/components";
import {
  computeComponentStats,
  type ComponentStats,
} from "@/lib/component-stats";
import { colorForMpcCommunity, detectMpcClusters } from "@/lib/mpc-detect";
import { stepPerComponentLayout } from "@/lib/per-component-layout";
import { classifyNodes, type NodeRole } from "@/lib/role-detect";

const MPC_DETECT_INTERVAL_MS = 3000;
const NEUTRAL_NODE_COLOR = "#d9c8a9";

/**
 * Owns a live graphology graph fed by the /graph/raw/stream SSE endpoint.
 * Every ingested transaction either:
 *   - adds a new wallet (positioned near its partner; hash-jittered if new),
 *   - updates an existing edge's volume/tx count,
 *   - or (self-loop) bumps a counter on the wallet.
 *
 * Incoming edges are queued into a ref; we flush once per animation frame
 * and mutate the shared Graph instance in place. React only re-renders the
 * status pill (`connected`, `edgeCount`), not the canvas.
 */
export function useRawStream() {
  const graphRef = useRef<Graph | null>(null);
  if (graphRef.current === null) {
    graphRef.current = new Graph({ multi: false, type: "undirected" });
  }

  const pendingRef = useRef<RawEdge[]>([]);
  const rafRef = useRef<number | null>(null);
  const filteredRef = useRef(0);
  // Union-Find over connected components. Drives teleport-on-merge
  // so two components bridged by a new edge snap together immediately
  // instead of relying on FA2 to pull them across the canvas.
  const componentsRef = useRef<ComponentState>(createComponentState());
  // Latest Louvain assignment, refreshed on the detect interval.
  // Layout uses it to push different communities apart even within a
  // single connected component so MPC sub-clusters don't sit on top of
  // their neighbors.
  const nodeToCommunityRef = useRef<Map<string, number>>(new Map());
  // Latest per-node role classification, recomputed each detect tick
  // from the current graph state. Sidecar to the graph itself so future
  // UIs can subscribe without recomputing. The same role is also
  // written onto each node as a `role` attribute for direct readers.
  const rolesRef = useRef<Map<string, NodeRole>>(new Map());
  // Latest per-component aggregates (size, totalVolume, top members,
  // role counts), keyed by Union-Find root id. Recomputed each detect
  // tick. Connected components are the most informative grouping in
  // raw blockchain data; this lets downstream views skip the walk.
  const componentStatsRef = useRef<Map<string, ComponentStats>>(new Map());
  const [status, setStatus] = useState<{
    connected: boolean;
    edgeCount: number;
    nodeCount: number;
    lagged: number;
    filtered: number;
  }>({
    connected: false,
    edgeCount: 0,
    nodeCount: 0,
    lagged: 0,
    filtered: 0,
  });

  useEffect(() => {
    const graph = graphRef.current!;

    const flush = () => {
      rafRef.current = null;
      const batch = pendingRef.current;
      if (batch.length === 0) return;
      pendingRef.current = [];
      for (const e of batch) {
        const acted = applyEdge(graph, e, componentsRef.current);
        if (!acted) filteredRef.current += 1;
      }
      setStatus((s) => ({
        ...s,
        edgeCount: graph.size,
        nodeCount: graph.order,
        filtered: filteredRef.current,
      }));
    };

    const schedule = () => {
      if (rafRef.current !== null) return;
      rafRef.current = requestAnimationFrame(flush);
    };

    const unsubscribe = subscribeRawStream(
      (edge) => {
        pendingRef.current.push(edge);
        schedule();
      },
      (missed) => {
        setStatus((s) => ({ ...s, lagged: s.lagged + missed }));
      },
      () => {
        setStatus((s) => ({ ...s, connected: false }));
      },
    );

    setStatus((s) => ({ ...s, connected: true }));

    // Per-component layout tick. Replaces the global FA2 worker so
    // forces only apply within each connected component  different
    // components sit at their deterministic spawn positions and never
    // drift toward each other under Barnes-Hut repulsion.
    let layoutRafId: number | null = null;
    const layoutTick = () => {
      layoutRafId = requestAnimationFrame(layoutTick);
      if (graph.order < 2) return;
      stepPerComponentLayout(
        graph,
        componentsRef.current,
        nodeToCommunityRef.current,
      );
    };
    layoutRafId = requestAnimationFrame(layoutTick);

    // Louvain + MPC scoring on a throttle. Runs on the main thread
    // because graphology-communities-louvain doesn't ship a worker; on
    // a 5k-node graph it's still <50ms so the dropped frame is
    // acceptable given the 3s cadence.
    const detectInterval = window.setInterval(() => {
      if (graph.order < 10) return;
      const { nodeToCommunity, mpcCommunities, communityStats } =
        detectMpcClusters(graph);
      nodeToCommunityRef.current = nodeToCommunity;
      graph.forEachNode((id) => {
        const c = nodeToCommunity.get(id);
        const color =
          c !== undefined && mpcCommunities.has(c)
            ? colorForMpcCommunity(c)
            : NEUTRAL_NODE_COLOR;
        graph.setNodeAttribute(id, "color", color);
      });
      if (mpcCommunities.size > 0) {
        // Sort by totalVolume rather than size: a 30-wallet cluster
        // moving 2,800 SOL is the interesting story, not a 200-wallet
        // dust loop holding 0.18 SOL. Volume is the fraud-relevant
        // signal once a community is already flagged by the heuristic.
        const flagged = [...mpcCommunities]
          .map((c) => ({ c, ...communityStats.get(c) }))
          .sort((a, b) => (b.totalVolume ?? 0) - (a.totalVolume ?? 0));
        // eslint-disable-next-line no-console
        console.log(
          "[mpc] " +
            JSON.stringify({
              flagged: flagged.length,
              top: flagged.slice(0, 5),
            }),
        );
      }

      // Top wallets by degree and volume. Used to diagnose the
      // "everything is one giant component" pattern  if one wallet
      // has >1000 edges it's almost certainly a DEX/exchange hot
      // wallet that's pulling thousands of unrelated counterparties
      // into a single Union-Find component, and we may want to let
      // the user filter it out.
      const allNodes: { id: string; degree: number; volume: number }[] = [];
      graph.forEachNode((id) => {
        allNodes.push({
          id,
          degree: (graph.getNodeAttribute(id, "degree") as number) ?? 0,
          volume: (graph.getNodeAttribute(id, "volume") as number) ?? 0,
        });
      });
      const topByDegree = [...allNodes]
        .sort((a, b) => b.degree - a.degree)
        .slice(0, 10)
        .map((n) => ({ id: n.id, degree: n.degree, volume: n.volume.toFixed(3) }));
      const topByVolume = [...allNodes]
        .sort((a, b) => b.volume - a.volume)
        .slice(0, 10)
        .map((n) => ({ id: n.id, volume: n.volume.toFixed(3), degree: n.degree }));
      // eslint-disable-next-line no-console
      console.log("[hubs] top by degree " + JSON.stringify(topByDegree));
      // eslint-disable-next-line no-console
      console.log("[hubs] top by volume " + JSON.stringify(topByVolume));

      // Per-cluster centrality diagnostic. For each connected
      // component above a minimum size, find the node with the
      // highest visibleDegree (the "biggest party") and the
      // runner-up. The ratio tells us whether the cluster has a
      // clear center (ratio >> 1 = star shape, biggest is
      // unambiguous) or no clear center (ratio ~= 1 = multi-hub or
      // mesh, biggest is a tie). We also report the biggest node's
      // distance from the cluster centroid so we can see whether
      // force balance is already placing it there or not.
      const clusters: Array<{
        size: number;
        top: string;
        topDeg: number;
        secondDeg: number;
        ratio: number;
        distFromCentroid: number;
      }> = [];
      for (const [, members] of componentsRef.current.members) {
        if (members.size < 5) continue;
        let cx = 0;
        let cy = 0;
        let topId = "";
        let topDeg = -1;
        let secondDeg = 0;
        for (const id of members) {
          cx += graph.getNodeAttribute(id, "x") as number;
          cy += graph.getNodeAttribute(id, "y") as number;
          const d =
            (graph.getNodeAttribute(id, "visibleDegree") as number) ?? 0;
          if (d > topDeg) {
            secondDeg = topDeg;
            topDeg = d;
            topId = id;
          } else if (d > secondDeg) {
            secondDeg = d;
          }
        }
        cx /= members.size;
        cy /= members.size;
        const tx = graph.getNodeAttribute(topId, "x") as number;
        const ty = graph.getNodeAttribute(topId, "y") as number;
        const distFromCentroid = Math.hypot(tx - cx, ty - cy);
        clusters.push({
          size: members.size,
          top: nodeLabel(topId),
          topDeg,
          secondDeg,
          ratio: secondDeg > 0 ? topDeg / secondDeg : Infinity,
          distFromCentroid: Math.round(distFromCentroid),
        });
      }
      clusters.sort((a, b) => b.size - a.size);
      // eslint-disable-next-line no-console
      console.log(
        "[clusters] centrality " + JSON.stringify(clusters.slice(0, 10)),
      );

      // Tip-account behavior profile. Jito tip accounts (and any
      // similar router/fee receivers) self-identify as: high degree,
      // tiny avg per-tx volume. We pick the top-N matching that
      // signature, then look at every wallet connected to at least
      // one of them: how many tips does it touch, what's its
      // in/out/bidir volume. This tells us the MEV-searcher
      // population shape without filtering anything out.
      // Tip-style signature: high degree + dust avg per edge. Loosened
      // from 0.001 to 0.01 SOL/edge after the data showed mega-routers
      // like 3dDx5... at degree 329 with 0.006 SOL/edge (memecoin
      // platform fee accounts and the like) were being missed.
      const tipCandidates = allNodes
        .filter((n) => {
          if (n.degree < 50) return false;
          const avgPerEdge = n.degree > 0 ? n.volume / n.degree : 0;
          return avgPerEdge < 0.01;
        })
        .sort((a, b) => b.degree - a.degree)
        .slice(0, 8)
        .map((n) => n.id);
      const tipSet = new Set(tipCandidates);
      const searcherProfile = new Map<
        string,
        { tipsTouched: number; otherDegree: number }
      >();
      for (const tipId of tipCandidates) {
        if (!graph.hasNode(tipId)) continue;
        graph.forEachNeighbor(tipId, (other) => {
          const cur = searcherProfile.get(other) ?? {
            tipsTouched: 0,
            otherDegree: 0,
          };
          cur.tipsTouched += 1;
          searcherProfile.set(other, cur);
        });
      }
      // Bucket searchers by how many tip accounts they touch.
      // 1   = one-off bundle, occasional MEV
      // 2-3 = part-time searcher
      // 4-6 = regular searcher
      // 7-8 = heavy MEV bot, paying every shift
      const buckets = { "1": 0, "2-3": 0, "4-6": 0, "7-8": 0 };
      for (const [, p] of searcherProfile) {
        if (p.tipsTouched === 1) buckets["1"]++;
        else if (p.tipsTouched <= 3) buckets["2-3"]++;
        else if (p.tipsTouched <= 6) buckets["4-6"]++;
        else buckets["7-8"]++;
      }
      // Heavy searchers: those touching >=4 tip accounts. For each,
      // show in/out/bidir balance + their non-tip counterparty count.
      // Heavy + balanced in/out + many non-tip neighbors = active
      // searcher cycling SOL through the system.
      // Heavy + outVol-only = paying tips, profits hidden in SPL
      // tokens we don't capture.
      const heavySearchers: Array<{
        id: string;
        tips: number;
        deg: number;
        nonTipDeg: number;
        inVol: string;
        outVol: string;
        bidirVol: string;
      }> = [];
      for (const [id, p] of searcherProfile) {
        if (p.tipsTouched < 4) continue;
        const deg = (graph.getNodeAttribute(id, "degree") as number) ?? 0;
        heavySearchers.push({
          id: nodeLabel(id),
          tips: p.tipsTouched,
          deg,
          nonTipDeg: deg - p.tipsTouched,
          inVol: ((graph.getNodeAttribute(id, "inVol") as number) ?? 0).toFixed(3),
          outVol: ((graph.getNodeAttribute(id, "outVol") as number) ?? 0).toFixed(3),
          bidirVol: ((graph.getNodeAttribute(id, "bidirVol") as number) ?? 0).toFixed(3),
        });
      }
      heavySearchers.sort((a, b) => b.tips - a.tips || b.deg - a.deg);
      // eslint-disable-next-line no-console
      console.log(
        "[mev] tip-style accounts " +
          JSON.stringify({
            count: tipCandidates.length,
            ids: tipCandidates.map(nodeLabel),
            buckets,
            uniqueSearchers: searcherProfile.size,
          }),
      );
      // eslint-disable-next-line no-console
      console.log(
        "[mev] heavy searchers " + JSON.stringify(heavySearchers.slice(0, 15)),
      );

      // Classify every node into one of six roles using the data we
      // already have on the graph. The raw graph stays raw; we just
      // tag it. Future UIs (wallet profile, MPC explorer, live MEV
      // dashboard) read the `role` attribute directly without
      // recomputing.
      const mpcMembers = new Set<string>();
      for (const [id, c] of nodeToCommunity) {
        if (mpcCommunities.has(c)) mpcMembers.add(id);
      }
      const tipsTouchedByNode = new Map<string, number>();
      for (const [id, p] of searcherProfile) {
        tipsTouchedByNode.set(id, p.tipsTouched);
      }
      const roles = classifyNodes({
        graph,
        tipAddrs: tipSet,
        mpcMembers,
        tipsTouchedByNode,
      });
      graph.forEachNode((id) => {
        graph.setNodeAttribute(id, "role", roles.get(id) ?? "normal");
      });
      rolesRef.current = roles;

      const componentStats = computeComponentStats(
        graph,
        componentsRef.current,
        roles,
      );
      componentStatsRef.current = componentStats;

      const roleSummary: Record<NodeRole, number> = {
        "tip-account": 0,
        "mev-searcher": 0,
        "flow-hub": 0,
        whale: 0,
        "mpc-member": 0,
        normal: 0,
      };
      for (const role of roles.values()) {
        roleSummary[role] += 1;
      }
      // eslint-disable-next-line no-console
      console.log("[roles] " + JSON.stringify(roleSummary));
    }, MPC_DETECT_INTERVAL_MS);

    return () => {
      unsubscribe();
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      window.clearInterval(detectInterval);
      if (layoutRafId !== null) cancelAnimationFrame(layoutRafId);
    };
  }, []);

  // graph is a stable singleton (created once on first render, never
  // reassigned). The two ref objects are also stable. Returning them
  // is intentional: callers read .current on demand outside render.
  // eslint-disable-next-line react-hooks/refs
  return {
    // eslint-disable-next-line react-hooks/refs
    graph: graphRef.current,
    status,
    rolesRef,
    componentStatsRef,
  };
}

/**
 * Deterministic hash → [0, 1). Used for jitter angles so a wallet id
 * always spawns at the same relative position given the same partner.
 */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

// Small jitter, not a real radius. New nodes are seeded right next to
// their partner so FA2 doesn't have to pull them across the canvas  it
// only has to separate two stacked points, which it does in a frame or
// two.
// Tiny jitter so a new node lands right on top of its partner and FA2
// just separates them  no cross-canvas travel.
const SPAWN_RADIUS = 1.5;
// Orphans scatter across a huge box so brand-new components start out
// far from every other component and don't have to push through the
// existing graph to claim their territory. Each id hashes to a stable
// (x, y) inside this box.
const ORPHAN_SPREAD = 10000;

function placeNear(
  graph: Graph,
  newId: string,
  partnerId: string | null,
): { x: number; y: number } {
  if (partnerId !== null && graph.hasNode(partnerId)) {
    const angle = hash01(newId) * Math.PI * 2;
    const px = graph.getNodeAttribute(partnerId, "x") as number;
    const py = graph.getNodeAttribute(partnerId, "y") as number;
    return {
      x: px + SPAWN_RADIUS * Math.cos(angle),
      y: py + SPAWN_RADIUS * Math.sin(angle),
    };
  }
  // Brand-new orphan: scatter randomly (but deterministically per id)
  // across a large area. FA2 will compact each component locally; what
  // we want to avoid is two components spawning on top of each other.
  // Prepend the axis tag so the differing byte is mixed through every
  // subsequent FNV step. Appending (id + ":x") barely changes the
  // output and lands every orphan on the y=x diagonal.
  const hx = hash01("x:" + newId);
  const hy = hash01("y:" + newId);
  return {
    x: (hx - 0.5) * ORPHAN_SPREAD,
    y: (hy - 0.5) * ORPHAN_SPREAD,
  };
}

function nodeLabel(id: string): string {
  return `${id.slice(0, 4)}…${id.slice(-4)}`;
}

function ensureNode(
  graph: Graph,
  id: string,
  partnerId: string | null,
  components: ComponentState,
): void {
  if (graph.hasNode(id)) return;
  const { x, y } = placeNear(graph, id, partnerId);
  addToComponent(components, id);
  graph.addNode(id, {
    x,
    y,
    size: 0.8,
    color: "#d9c8a9",
    label: nodeLabel(id),
    degree: 0,
    // Only edges that have actually become visible (txCount >=
    // MIN_EDGE_TX_COUNT) count toward size. A wallet with 50 hidden
    // one-off dust pairs shouldn't render as a hub.
    visibleDegree: 0,
    volume: 0,
    selfLoops: 0,
    // MPC signal inputs. inVol/outVol feed the balanced-flow ratio;
    // bidirVol counts volume on edges that have been observed in both
    // directions. These sit unused in the layout today and are read by
    // the (upcoming) MPC detection pass.
    inVol: 0,
    outVol: 0,
    // Derived classification, set every detect tick by classifyNodes.
    // Default "normal" so reads before the first detect run don't
    // crash on undefined.
    role: "normal" as NodeRole,
    bidirVol: 0,
  });
}

/// Below this SOL volume, a brand-new pair (both endpoints unseen)
/// is treated as dust and skipped. If either endpoint is already in
/// the graph, we always include it  that's a fan-out signal and not
/// noise. 0.001 SOL = 1M lamports; the Solana median transfer in our
/// stream is ~0.0001 SOL, so this threshold filters the long tail of
/// dust-only pairs without hiding real flow involving an active wallet.
const MIN_LONELY_VOLUME = 0.001;

/// Returns true if the edge was applied (rendered), false if filtered
/// out as noise. Self-loops on a known wallet always count.
function applyEdge(
  graph: Graph,
  e: RawEdge,
  components: ComponentState,
): boolean {
  // Self-loops: no geometric meaning, but surface them on the node so
  // bot/spam wallets still show up. Filter dust self-loops on brand-new
  // wallets for the same reason as regular edges.
  if (e.from === e.to) {
    if (!graph.hasNode(e.from) && e.volume_sol < MIN_LONELY_VOLUME) {
      return false;
    }
    ensureNode(graph, e.from, null, components);
    const cur = (graph.getNodeAttribute(e.from, "selfLoops") as number) + 1;
    graph.setNodeAttribute(e.from, "selfLoops", cur);
    graph.setNodeAttribute(e.from, "size", nodeSize(graph, e.from));
    return true;
  }

  // Dust filter: if BOTH endpoints are brand-new AND volume is tiny,
  // skip entirely. This kills one-off wallet-pair dust without hiding
  // any transaction that touches an already-known (busy) wallet.
  const fromExists = graph.hasNode(e.from);
  const toExists = graph.hasNode(e.to);
  if (!fromExists && !toExists && e.volume_sol < MIN_LONELY_VOLUME) {
    return false;
  }

  // Edges start hidden until txCount crosses MIN_EDGE_TX_COUNT. We
  // defer every visual side-effect of the edge until that crossing
  // spawn placement, union-find merge, attraction force, layout
  // teleport. Otherwise a brand-new orphan would land on top of its
  // future partner before there's any visible line connecting them,
  // and the layout would silently pull components together for edges
  // the user can't see. "Edge appears, then node moves," not the
  // other way around.
  const edgeStartsHidden = MIN_EDGE_TX_COUNT > 1;
  if (!fromExists && !toExists) {
    // Brand-new pair: spawn together regardless. They form their own
    // tiny component at an orphan-spread position; no other component
    // is involved.
    ensureNode(graph, e.from, null, components);
    ensureNode(graph, e.to, e.from, components);
  } else if (!fromExists) {
    // Only e.to exists. If the edge starts hidden, spawn the new node
    // at orphan-spread (far from everything) so it doesn't visually
    // teleport to the cluster before the edge is real.
    ensureNode(graph, e.from, edgeStartsHidden ? null : e.to, components);
  } else if (!toExists) {
    ensureNode(graph, e.to, edgeStartsHidden ? null : e.from, components);
  }

  // Node volume accounting, split by direction so we can compute a
  // balanced-flow ratio per wallet. MPC hot wallets keep in~=out;
  // accumulators skew heavily one way.
  incAttr(graph, e.from, "volume", e.volume_sol);
  incAttr(graph, e.to, "volume", e.volume_sol);
  incAttr(graph, e.from, "outVol", e.volume_sol);
  incAttr(graph, e.to, "inVol", e.volume_sol);

  // Edge: thicken on repeats. graphology is undirected + simple, so
  // hasEdge handles both directions.
  if (graph.hasEdge(e.from, e.to)) {
    const eid = graph.edge(e.from, e.to)!;
    const wasHidden = graph.getEdgeAttribute(eid, "hidden") as boolean;
    incAttr(graph, eid, "volume", e.volume_sol, "edge");
    incAttr(graph, eid, "txCount", 1, "edge");
    bumpDirection(graph, eid, e);
    graph.setEdgeAttribute(
      eid,
      "size",
      edgeWidth(graph.getEdgeAttribute(eid, "volume") as number, graph, e.from, e.to),
    );
    graph.setEdgeAttribute(eid, "weight", graph.getEdgeAttribute(eid, "txCount") as number);
    const txCount = graph.getEdgeAttribute(eid, "txCount") as number;
    if (wasHidden && txCount >= MIN_EDGE_TX_COUNT) {
      // Edge just crossed the visibility threshold. This is where all
      // the deferred work happens: union the components, migrate the
      // loser's members to the winner's anchor, freeze the edge color
      // against the post-migration positions, and unhide. The order
      // matters  migrate first so the color is computed against the
      // final node positions, otherwise it'd be picked from the old
      // distant orphan-spread location and read as a long faint edge
      // even though the cluster is now compact.
      commitVisibility(graph, components, e.from, e.to, eid);
    }
  } else {
    // Canonical direction = the "from" of the very first observation.
    // Later txs are classified as AB (matches canonical) or BA (reverse).
    const startsHidden = MIN_EDGE_TX_COUNT > 1;
    graph.addEdge(e.from, e.to, {
      volume: e.volume_sol,
      txCount: 1,
      weight: 1,
      canonicalFrom: e.from,
      volAB: e.volume_sol,
      volBA: 0,
      txAB: 1,
      txBA: 0,
      size: edgeWidth(e.volume_sol, graph, e.from, e.to),
      color: computeEdgeColor(graph, e.from, e.to),
      hidden: startsHidden,
    });
    // First edge for either endpoint bumps total degree. visibleDegree
    // only bumps if the edge is visible from the start (which it isn't
    // when MIN_EDGE_TX_COUNT > 1).
    incAttr(graph, e.from, "degree", 1);
    incAttr(graph, e.to, "degree", 1);
    if (!startsHidden) {
      // Edge is visible from creation (MIN_EDGE_TX_COUNT == 1). Run
      // the same union+migrate+visibleDegree work that the visibility
      // crossing branch does, since there's nothing to defer.
      const eid = graph.edge(e.from, e.to)!;
      commitVisibility(graph, components, e.from, e.to, eid);
    } else {
      graph.setNodeAttribute(e.from, "size", nodeSize(graph, e.from));
      graph.setNodeAttribute(e.to, "size", nodeSize(graph, e.to));
      refreshEdgeSizes(graph, e.from);
      refreshEdgeSizes(graph, e.to);
      refreshNodeHidden(graph, e.from);
      refreshNodeHidden(graph, e.to);
    }
  }
  return true;
}

// Record the tx against the canonical direction of the edge and promote
// the edge to "bidirectional" the first time we see traffic both ways.
// Flipping an edge to bidir shifts its volume into the bidirVol counter
// on both endpoints, which the MPC detector weighs heavily.
function bumpDirection(graph: Graph, eid: string, e: RawEdge): void {
  const canonicalFrom = graph.getEdgeAttribute(eid, "canonicalFrom") as string;
  const wasBidir =
    (graph.getEdgeAttribute(eid, "txAB") as number) > 0 &&
    (graph.getEdgeAttribute(eid, "txBA") as number) > 0;
  if (e.from === canonicalFrom) {
    incAttr(graph, eid, "volAB", e.volume_sol, "edge");
    incAttr(graph, eid, "txAB", 1, "edge");
  } else {
    incAttr(graph, eid, "volBA", e.volume_sol, "edge");
    incAttr(graph, eid, "txBA", 1, "edge");
  }
  const isBidir =
    (graph.getEdgeAttribute(eid, "txAB") as number) > 0 &&
    (graph.getEdgeAttribute(eid, "txBA") as number) > 0;
  if (!wasBidir && isBidir) {
    // Just crossed the bidirectional threshold. Back-credit the edge's
    // full volume to both endpoints' loop-volume pots  every tx on this
    // edge now counts as closed-loop.
    const v = graph.getEdgeAttribute(eid, "volume") as number;
    incAttr(graph, e.from, "bidirVol", v);
    incAttr(graph, e.to, "bidirVol", v);
  } else if (isBidir) {
    // Ongoing bidirectional edge: just this tx's volume flows into the
    // loop pot.
    incAttr(graph, e.from, "bidirVol", e.volume_sol);
    incAttr(graph, e.to, "bidirVol", e.volume_sol);
  }
}

// Long edges are almost always the visually-noisy cross-canvas
// crossings. Blend their color toward the canvas background so they
// disappear into the backdrop; short intra-cluster edges keep their
// full color.
//
const EDGE_ALPHA_FULL = 0.55;
// Long edges dim but stay visible. Going below ~0.2 on black makes
// them read as gone rather than "secondary" which isn't what we want.
const EDGE_ALPHA_DIM = 0.22;
// One-off pairs are the bulk of the clutter  they're neither recurring
// behavior nor hubs, just single transfers. Hide them once they're no
// longer the freshest signal. Edges unlock again the moment they get a
// second tx.
const MIN_EDGE_TX_COUNT = 2;
// Absolute world-space cutoff beyond which an edge is treated as
// "long" and rendered dim. Fixed, not a moving percentile, because a
// percentile-based cutoff made boundary edges blink between bright and
// dim every tick as FA2 shifted lengths.
const EDGE_LONG_CUTOFF = 3000;

// Edge color is computed once at the moment the edge becomes visible
// and then left alone. FA2 will later stretch or squeeze that edge,
// but re-evaluating every tick produced visible blinking that was
// worse than any information gained from a live color. "Once formed,
// it's formed."
function computeEdgeColor(graph: Graph, src: string, tgt: string): string {
  const sx = graph.getNodeAttribute(src, "x") as number;
  const sy = graph.getNodeAttribute(src, "y") as number;
  const tx = graph.getNodeAttribute(tgt, "x") as number;
  const ty = graph.getNodeAttribute(tgt, "y") as number;
  const len = Math.hypot(tx - sx, ty - sy);
  const t = Math.min(1, len / EDGE_LONG_CUTOFF);
  const alpha = EDGE_ALPHA_FULL + (EDGE_ALPHA_DIM - EDGE_ALPHA_FULL) * t;
  return `rgba(200,210,235,${alpha.toFixed(3)})`;
}

// A node is visible iff any of its edges is visible or it has a
// self-loop signal. Called at the two moments edge visibility can
// change: initial edge creation and the tx-count crossing.
function refreshNodeHidden(graph: Graph, nodeId: string): void {
  let hasVisibleEdge = false;
  graph.forEachEdge(nodeId, (_eid, attrs) => {
    if (!(attrs.hidden as boolean)) hasVisibleEdge = true;
  });
  const selfLoops = (graph.getNodeAttribute(nodeId, "selfLoops") as number) ?? 0;
  graph.setNodeAttribute(nodeId, "hidden", !hasVisibleEdge && selfLoops === 0);
}

// Single point where an edge becomes "real": union the components,
// migrate the loser's members onto the winner's anchor, freeze the
// edge color, unhide the edge, and bump the visible-degree-driven
// sizes. Called both at first creation when MIN_EDGE_TX_COUNT == 1
// and at the txCount crossing otherwise.
function commitVisibility(
  graph: Graph,
  components: ComponentState,
  fromId: string,
  toId: string,
  eid: string,
): void {
  // Union + migration first so the freeze color is computed against
  // the final positions, not the pre-migration distant ones.
  const rootA = findRoot(components, fromId);
  const rootB = findRoot(components, toId);
  if (rootA !== rootB) {
    const merge = union(components, fromId, toId);
    if (merge.merged) {
      const anchor = merge.winner === rootA ? fromId : toId;
      migrateMembersToAnchor(graph, merge.migrated, anchor);
    }
  }
  graph.setEdgeAttribute(eid, "color", computeEdgeColor(graph, fromId, toId));
  graph.setEdgeAttribute(eid, "hidden", false);
  incAttr(graph, fromId, "visibleDegree", 1);
  incAttr(graph, toId, "visibleDegree", 1);
  graph.setNodeAttribute(fromId, "size", nodeSize(graph, fromId));
  graph.setNodeAttribute(toId, "size", nodeSize(graph, toId));
  refreshEdgeSizes(graph, fromId);
  refreshEdgeSizes(graph, toId);
  refreshNodeHidden(graph, fromId);
  refreshNodeHidden(graph, toId);
}

// Teleport every member of a just-merged component to the vicinity of
// the anchor node (which lives in the surviving component). Each
// member gets a tiny deterministic offset so they don't all stack on
// exactly the same point  FA2 would then waste its time untangling a
// degenerate overlap.
function migrateMembersToAnchor(
  graph: Graph,
  members: string[],
  anchor: string,
): void {
  const ax = graph.getNodeAttribute(anchor, "x") as number;
  const ay = graph.getNodeAttribute(anchor, "y") as number;
  for (const id of members) {
    if (id === anchor) continue;
    const angle = hash01(id) * Math.PI * 2;
    const r = SPAWN_RADIUS * (1 + hash01("r:" + id));
    graph.setNodeAttribute(id, "x", ax + r * Math.cos(angle));
    graph.setNodeAttribute(id, "y", ay + r * Math.sin(angle));
  }
}

// When a node crosses from degree 1 to 2, its previously-lonely edge
// is now hub-adjacent and should be free to scale with volume.
function refreshEdgeSizes(graph: Graph, nodeId: string): void {
  graph.forEachEdge(nodeId, (eid, attrs, source, target) => {
    const vol = attrs.volume as number;
    graph.setEdgeAttribute(eid, "size", edgeWidth(vol, graph, source, target));
  });
}

function incAttr(
  graph: Graph,
  id: string,
  key: string,
  delta: number,
  kind: "node" | "edge" = "node",
): void {
  if (kind === "node") {
    const cur = (graph.getNodeAttribute(id, key) as number) ?? 0;
    graph.setNodeAttribute(id, key, cur + delta);
  } else {
    const cur = (graph.getEdgeAttribute(id, key) as number) ?? 0;
    graph.setEdgeAttribute(id, key, cur + delta);
  }
}

// Min-max normalization with a hard cap. Keeps hubs clearly the
// largest nodes on screen without letting them grow so big they
// occlude their neighbors. Reference degree = where a node hits max
// size; anything higher is clamped.
const NODE_SIZE_MIN_PX = 1.5;
const NODE_SIZE_MAX_PX = 10;
const NODE_SIZE_REF_DEGREE = 60;

function nodeSize(graph: Graph, id: string): number {
  const degree = (graph.getNodeAttribute(id, "visibleDegree") as number) ?? 0;
  if (degree <= 1) return 0.8;
  const norm = Math.min(1, (degree - 1) / (NODE_SIZE_REF_DEGREE - 1));
  return NODE_SIZE_MIN_PX + Math.sqrt(norm) * (NODE_SIZE_MAX_PX - NODE_SIZE_MIN_PX);
}

function edgeWidth(
  _volumeSol: number,
  _graph: Graph,
  _from: string,
  _to: string,
): number {
  // Uniform thickness  volume is expressed through node size + color,
  // not edge width.
  return 0.6;
}
