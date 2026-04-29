"use client";

import Graph from "graphology";
import { useEffect, useRef, useState } from "react";
import type { RawEdge } from "@/lib/api";
import {
  addNode as addToComponent,
  createComponentState,
  findRoot,
  handleEdgeRemoved,
  removeNode as removeFromComponent,
  union,
  type Adjacency,
  type ComponentState,
} from "@/lib/components";
import {
  computeComponentStats,
  type ComponentStats,
} from "@/lib/component-stats";
import { detectMpcClusters } from "@/lib/mpc-detect";
import {
  createWorkerLayoutClient,
  type LayoutClient,
} from "@/lib/layout-client";
import { classifyNodes, type NodeRole } from "@/lib/role-detect";
import { colorForEdgeKind, colorForRole, ROLE_PALETTE } from "@/lib/role-colors";

const DEFAULT_API_URL = "http://localhost:8002";

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

// Detection interval for the Louvain + role-classification pass.
const MPC_DETECT_INTERVAL_MS = 3000;

export type RoleSummary = Record<NodeRole, number>;

/** Rolling-window sizes the backend supports on `/graph/stream?window=`. */
export type WindowSeconds = 10 | 60 | 300 | 900 | 1800 | 3600;
export const WINDOW_SECONDS: readonly WindowSeconds[] = [10, 60, 300, 900, 1800, 3600] as const;
export const DEFAULT_WINDOW_SECONDS: WindowSeconds = 60;

/**
 * Owns a live graphology graph fed by the /graph/stream SSE endpoint
 * (delta protocol). Consumes NodeAdded + EdgeAdded deltas, mapping
 * u32 node indices to pubkeys, then feeds the resulting RawEdge-shaped
 * objects into the same applyEdge path as slice 1. Backend graph state
 * is a "shadow"; frontend computes its own Union-Find / layout / roles.
 *
 * Every ingested transaction either:
 *   - adds a new wallet (positioned near its partner; hash-jittered if new),
 *   - updates an existing edge's volume/tx count,
 *   - or (self-loop) bumps a counter on the wallet.
 *
 * Incoming edges are queued into a ref; we flush once per animation frame
 * and mutate the shared Graph instance in place. React only re-renders the
 * status pill (`connected`, `edgeCount`), not the canvas.
 */
export function useRawStream({
  windowSecs = DEFAULT_WINDOW_SECONDS,
}: { windowSecs?: WindowSeconds } = {}) {
  const graphRef = useRef<Graph | null>(null);
  if (graphRef.current === null) {
    graphRef.current = new Graph({ multi: false, type: "undirected" });
  }

  const pendingRef = useRef<RawEdge[]>([]);
  const rafRef = useRef<number | null>(null);
  // Worker-backed layout client. The worker owns positions
  // authoritatively across frames; main sends deltas (addNode,
  // addEdge, bumpEdgeWeight, removeNode, removeEdge, setRole,
  // setCommunity) and consumes positions one-way via
  // `applyLatestPositions`. Constructed in a client-only `useEffect`
  // because Next.js renders client components on the server too,
  // where `Worker` is undefined.
  const layoutClientRef = useRef<LayoutClient | null>(null);
  // Slot id assignment for the layout client. Slots are u32,
  // monotonically increasing, never reused. At sustained 200 nodes/s
  // wraparound is irrelevant.
  const slotByPubkeyRef = useRef<Map<string, number>>(new Map());
  const pubkeyBySlotRef = useRef<Map<number, string>>(new Map());
  const nextSlotRef = useRef<number>(0);
  // Union-Find over connected components. Drives teleport-on-merge
  // so two components bridged by a new edge snap together immediately
  // instead of relying on FA2 to pull them across the canvas.
  const componentsRef = useRef<ComponentState>(createComponentState());
  // Latest Louvain assignment, refreshed on the detect interval.
  const nodeToCommunityRef = useRef<Map<string, number>>(new Map());
  // Latest per-node role classification, recomputed each detect tick.
  const rolesRef = useRef<Map<string, NodeRole>>(new Map());
  // Set of pubkeys observed as the synthetic peer on a mint or burn edge.
  const mintAddrsRef = useRef<Set<string>>(new Set());
  // Latest per-component aggregates, keyed by Union-Find root id.
  const componentStatsRef = useRef<Map<string, ComponentStats>>(new Map());
  // NodeIdx (u32) -> pubkey map populated from NodeAdded deltas.
  const idxToPubkeyRef = useRef<Map<number, string>>(new Map());
  // True when the next reconnect should ask the backend to skip the
  // bootstrap replay (only set by the explicit "Reset from now" button;
  // window-change reconnects keep bootstrap on so the new window's
  // historical edges populate the graph).
  const skipBootstrapNextRef = useRef<boolean>(false);
  const [status, setStatus] = useState<{
    connected: boolean;
    edgeCount: number;
    nodeCount: number;
    lagged: number;
    /** Latest ingested block_time (Unix seconds), or null until first poll. */
    latestBlockTime: number | null;
    /** Block-time span (seconds) between oldest and newest live edge,
     *  capped by the 3600s rolling buffer. Null until first poll. */
    accumulatedSecs: number | null;
  }>({
    connected: false,
    edgeCount: 0,
    nodeCount: 0,
    lagged: 0,
    latestBlockTime: null,
    accumulatedSecs: null,
  });
  const [roleSummary, setRoleSummary] = useState<RoleSummary>({
    "token-mint": 0,
    "tip-account": 0,
    "mev-searcher": 0,
    "multi-hub": 0,
    "sol-hub": 0,
    "spl-hub": 0,
    whale: 0,
    "mpc-member": 0,
    normal: 0,
  });
  // Increment to trigger useEffect re-run (new EventSource).
  const [resetTick, setResetTick] = useState(0);

  // Clear all locally-derived state. Shared between the explicit
  // reset() click and the window-change reconnect path.
  const clearLocalState = () => {
    const graph = graphRef.current!;
    graph.clear();
    componentsRef.current = createComponentState();
    mintAddrsRef.current = new Set();
    idxToPubkeyRef.current = new Map();
    nodeToCommunityRef.current = new Map();
    rolesRef.current = new Map();
    componentStatsRef.current = new Map();
    pendingRef.current = [];
    slotByPubkeyRef.current = new Map();
    pubkeyBySlotRef.current = new Map();
    nextSlotRef.current = 0;
    layoutClientRef.current?.reset();
    setStatus({
      connected: false,
      edgeCount: 0,
      nodeCount: 0,
      lagged: 0,
      latestBlockTime: null,
      accumulatedSecs: null,
    });
    setRoleSummary({
      "token-mint": 0,
      "tip-account": 0,
      "mev-searcher": 0,
      "multi-hub": 0,
      "sol-hub": 0,
      "spl-hub": 0,
      whale: 0,
      "mpc-member": 0,
      normal: 0,
    });
  };

  // Construct the worker-backed layout client on mount; terminate
  // the worker on unmount. Empty deps: re-mounts (StrictMode) build
  // a fresh worker. Window-switch / reset don't recreate it; they
  // call `.reset()` to drop the worker's internal state.
  useEffect(() => {
    layoutClientRef.current = createWorkerLayoutClient();
    return () => {
      layoutClientRef.current?.terminate?.();
      layoutClientRef.current = null;
    };
  }, []);

  // reset(): bump the tick. The main effect below clears state and
  // opens a fresh SSE; with `skipBootstrapNextRef` set, the new
  // connection asks the backend to skip cold-start replay so only live
  // edges arrive.
  const reset = () => {
    skipBootstrapNextRef.current = true;
    setResetTick((n) => n + 1);
  };

  useEffect(() => {
    // Single effect for both window change and reset. Cleanup of the
    // previous run closes the old SSE BEFORE the new body runs, so
    // there's no gap where the old window's broadcast can leak events
    // into the cleared graph.
    clearLocalState();

    const graph = graphRef.current!;
    const idxToPubkey = idxToPubkeyRef.current;
    const skipBootstrap = skipBootstrapNextRef.current;
    skipBootstrapNextRef.current = false;

    // Wallclock budget per frame instead of fixed edge count. Each
    // `applyEdge` call cost varies (megahub bumps are cheap; new
    // nodes hit `ensureNode`'s centroid-spawn path which iterates
    // component members). Time-budgeting keeps every frame within
    // FLUSH_BUDGET_MS of `applyEdge` work regardless of which edge
    // mix lands, leaving the rest of the frame for Sigma's render
    // and user events. Steady-state at 405 tx/sec is ~6.5 edges
    // per RAF, far under the budget, so this never trips in normal
    // flow.
    const FLUSH_BUDGET_MS = 6;
    const flush = () => {
      rafRef.current = null;
      const queue = pendingRef.current;
      if (queue.length === 0) return;
      const dispatch: LayoutDispatch | null = layoutClientRef.current
        ? {
            client: layoutClientRef.current,
            slotByPubkey: slotByPubkeyRef.current,
            pubkeyBySlot: pubkeyBySlotRef.current,
            nextSlot: nextSlotRef,
          }
        : null;
      const start = performance.now();
      let i = 0;
      while (
        i < queue.length &&
        performance.now() - start < FLUSH_BUDGET_MS
      ) {
        applyEdge(
          graph,
          queue[i],
          componentsRef.current,
          mintAddrsRef.current,
          dispatch,
        );
        i++;
      }
      if (i === queue.length) {
        pendingRef.current = [];
      } else {
        pendingRef.current = queue.slice(i);
        if (rafRef.current === null) {
          rafRef.current = requestAnimationFrame(flush);
        }
      }
      setStatus((s) => ({
        ...s,
        edgeCount: graph.size,
        nodeCount: graph.order,
      }));
    };

    const schedule = () => {
      if (rafRef.current !== null) return;
      rafRef.current = requestAnimationFrame(flush);
    };

    // Open SSE connection to the delta-protocol endpoint.
    const url = new URL("/graph/stream", apiUrl());
    url.searchParams.set("window", String(windowSecs));
    if (skipBootstrap) {
      url.searchParams.set("skip_bootstrap", "1");
    }
    const es = new EventSource(url.toString());

    es.onopen = () => {
      setStatus((s) => ({ ...s, connected: true }));
    };

    es.onerror = () => {
      setStatus((s) => ({ ...s, connected: false }));
    };

    // NodeAdded: populate the idx->pubkey map. Do not add to graphology
    // yet; defer until EdgeAdded so we can place the node near its partner.
    es.addEventListener("NodeAdded", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        idxToPubkey.set(d.idx as number, d.pubkey as string);
      } catch {
        // ignore malformed events
      }
    });

    es.addEventListener("EdgeAdded", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        const from = idxToPubkey.get(d.src as number);
        const to = idxToPubkey.get(d.dst as number);
        if (!from || !to) return;
        // Build a RawEdge-shaped object. The signature is the full
        // backend handle: `${slotIdx}:${gen}`. Generation is bumped
        // every time the slot is reused, so two edges that happen to
        // share an idx never share a signature.
        const edge: RawEdge = {
          signature: `${d.idx}:${d.gen}`,
          block_time: Number(d.slot),      // slot as monotonic timestamp surrogate
          from,
          to,
          volume_sol: d.mint ? 0 : Number(d.amount) / 1e9, // LAMPORTS_PER_SOL = 1e9
          mint: d.mint ?? undefined,
          kind: d.kind ?? undefined,
        };
        pendingRef.current.push(edge);
        schedule();
      } catch {
        // ignore malformed events
      }
    });

    es.addEventListener("EdgeExpired", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        const edgeKey = `${d.idx}:${d.gen}`;
        if (!graph.hasEdge(edgeKey)) return;
        const src = graph.source(edgeKey);
        const dst = graph.target(edgeKey);
        graph.dropEdge(edgeKey);
        // Decrement degrees on both endpoints if still present.
        if (graph.hasNode(src)) {
          graph.updateNodeAttribute(src, "degree", (n) => Math.max(0, (n ?? 1) - 1));
        }
        if (graph.hasNode(dst)) {
          graph.updateNodeAttribute(dst, "degree", (n) => Math.max(0, (n ?? 1) - 1));
        }
        // Tell the layout client to drop the edge from its mirror.
        const srcSlot = slotByPubkeyRef.current.get(src);
        const dstSlot = slotByPubkeyRef.current.get(dst);
        if (
          srcSlot !== undefined &&
          dstSlot !== undefined &&
          layoutClientRef.current
        ) {
          layoutClientRef.current.removeEdge(srcSlot, dstSlot);
        }
        // Detect connectivity loss (the edge may have been a bridge)
        // and split the component if so. Adjacency lookup goes against
        // graphology, which has already had the edge dropped above, so
        // the BFS walks the post-removal world. Result: window-correct
        // components without rebuild-from-scratch  cost is bounded by
        // the smaller side, often a few hops when the edge wasn't
        // load-bearing.
        const adj: Adjacency = (id) => graph.neighbors(id);
        handleEdgeRemoved(componentsRef.current, src, dst, adj);
        setStatus((s) => ({ ...s, edgeCount: graph.size, nodeCount: graph.order }));
      } catch {
        // ignore malformed events
      }
    });

    es.addEventListener("NodeExpired", (ev) => {
      try {
        const d = JSON.parse((ev as MessageEvent).data);
        const pubkey = idxToPubkey.get(d.idx as number);
        if (!pubkey) return;
        if (graph.hasNode(pubkey)) {
          // Defensive: graphology's dropNode throws if the node still
          // has incident edges. Backend orders EdgeExpired before
          // NodeExpired, but a dropped/out-of-order delta would leave
          // stragglers; clear them so dropNode can't fail.
          for (const eid of graph.edges(pubkey)) {
            graph.dropEdge(eid);
          }
          graph.dropNode(pubkey);
        }
        idxToPubkey.delete(d.idx as number);
        // Clean up every client-side ref that keyed off this pubkey,
        // otherwise the layout, role legend, and ensureNode centroid
        // path keep referencing a dead node and either crash or paint
        // ghosts.
        removeFromComponent(componentsRef.current, pubkey);
        mintAddrsRef.current.delete(pubkey);
        nodeToCommunityRef.current.delete(pubkey);
        rolesRef.current.delete(pubkey);
        // Drop the layout client's mirror of this node and release
        // its slot id (slot ids are not reused, but we clear the
        // pubkey<->slot maps so future references to this pubkey
        // get a fresh slot when the wallet re-appears).
        const slot = slotByPubkeyRef.current.get(pubkey);
        if (slot !== undefined) {
          layoutClientRef.current?.removeNode(slot);
          slotByPubkeyRef.current.delete(pubkey);
          pubkeyBySlotRef.current.delete(slot);
        }
        setStatus((s) => ({ ...s, edgeCount: graph.size, nodeCount: graph.order }));
      } catch {
        // ignore malformed events
      }
    });

    es.addEventListener("CaughtUp", () => {
      // Bootstrap replay complete. Connected status is already true.
      // Could set a "loaded" flag here if needed.
    });

    // Once-per-RAF position applier. The layout client owns physics
    // state (worker thread); main's job here is just to drain the
    // most recent positions onto graphology so Sigma renders them.
    //
    // While the SSE flush queue is heavy (bootstrap), skip the
    // writeback. Graphology positions stay at their initial spawn
    // values; Sigma keeps painting those without the per-frame
    // setNodeAttribute storm + render trigger that the worker's
    // diff would cause. Positions converge once the queue drains
    // under the threshold; nodes snap to physics-stable layout in
    // one frame instead of jittering through bootstrap.
    const POSITION_WRITEBACK_QUEUE_THRESHOLD = 200;
    let layoutRafId: number | null = null;
    const layoutTick = () => {
      layoutRafId = requestAnimationFrame(layoutTick);
      const client = layoutClientRef.current;
      if (client === null) return;
      if (
        pendingRef.current.length > POSITION_WRITEBACK_QUEUE_THRESHOLD
      ) {
        return;
      }
      client.applyLatestPositions(graph, pubkeyBySlotRef.current);
    };
    layoutRafId = requestAnimationFrame(layoutTick);

    // Louvain + MPC scoring on a throttle.
    const detectInterval = window.setInterval(() => {
      if (graph.order < 10) return;
      const { nodeToCommunity, mpcCommunities, communityStats } =
        detectMpcClusters(graph);
      nodeToCommunityRef.current = nodeToCommunity;
      if (mpcCommunities.size > 0) {
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
            (graph.getNodeAttribute(id, "degree") as number) ?? 0;
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
      const buckets = { "1": 0, "2-3": 0, "4-6": 0, "7-8": 0 };
      for (const [, p] of searcherProfile) {
        if (p.tipsTouched === 1) buckets["1"]++;
        else if (p.tipsTouched <= 3) buckets["2-3"]++;
        else if (p.tipsTouched <= 6) buckets["4-6"]++;
        else buckets["7-8"]++;
      }
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
        mintAddrs: mintAddrsRef.current,
        tipsTouchedByNode,
      });
      const summary: RoleSummary = {
        "token-mint": 0,
        "tip-account": 0,
        "mev-searcher": 0,
        "multi-hub": 0,
        "sol-hub": 0,
        "spl-hub": 0,
        whale: 0,
        "mpc-member": 0,
        normal: 0,
      };
      const client = layoutClientRef.current;
      const slotByPubkey = slotByPubkeyRef.current;
      graph.forEachNode((id) => {
        const role = roles.get(id) ?? "normal";
        graph.setNodeAttribute(id, "role", role);
        graph.setNodeAttribute(id, "color", colorForRole(role));
        summary[role] += 1;
        if (client !== null) {
          const slot = slotByPubkey.get(id);
          if (slot !== undefined) {
            client.setRole(slot, role);
            const community = nodeToCommunity.get(id) ?? -1;
            client.setCommunity(slot, community);
          }
        }
      });
      rolesRef.current = roles;
      setRoleSummary(summary);

      const componentStats = computeComponentStats(
        graph,
        componentsRef.current,
        roles,
      );
      componentStatsRef.current = componentStats;

      // eslint-disable-next-line no-console
      console.log("[roles] " + JSON.stringify(summary));
    }, MPC_DETECT_INTERVAL_MS);

    return () => {
      es.close();
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      window.clearInterval(detectInterval);
      if (layoutRafId !== null) cancelAnimationFrame(layoutRafId);
      setStatus((s) => ({ ...s, connected: false }));
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowSecs, resetTick]);

  // Tip poll: hit /graph/stats every 3s for the latest ingested
  // block_time. The SSE delta protocol doesn't carry block_time on
  // EdgeAdded (it carries `slot` only), so polling is the simplest
  // path to keep a chain-tip indicator current.
  useEffect(() => {
    let cancelled = false;
    const fetchTip = async () => {
      try {
        const res = await fetch(`${apiUrl()}/graph/stats?window=3600`);
        if (!res.ok) return;
        const j = (await res.json()) as {
          latest_block_time?: number;
          accumulated_secs?: number;
        };
        if (cancelled || typeof j.latest_block_time !== "number") return;
        const nextLatest = j.latest_block_time;
        const nextAccum =
          typeof j.accumulated_secs === "number" ? j.accumulated_secs : null;
        setStatus((s) =>
          s.latestBlockTime === nextLatest && s.accumulatedSecs === nextAccum
            ? s
            : { ...s, latestBlockTime: nextLatest, accumulatedSecs: nextAccum },
        );
      } catch {
        // ignore network blips; next tick retries
      }
    };
    fetchTip();
    const id = window.setInterval(fetchTip, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return {
    graph: graphRef.current,
    status,
    roleSummary,
    rolesRef,
    componentStatsRef,
    reset,
  };
}

/**
 * Deterministic hash -> [0, 1). Used for jitter angles so a wallet id
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
  // across a large area.
  const hx = hash01("x:" + newId);
  const hy = hash01("y:" + newId);
  return {
    x: (hx - 0.5) * ORPHAN_SPREAD,
    y: (hy - 0.5) * ORPHAN_SPREAD,
  };
}

function nodeLabel(id: string): string {
  return `${id.slice(0, 4)}...${id.slice(-4)}`;
}

// Minimum number of component members already in the graph before we
// prefer a component centroid over a partner position. Below this
// threshold the centroid is too noisy to be useful.
const CENTROID_THRESHOLD = 4;

interface LayoutDispatch {
  client: LayoutClient;
  slotByPubkey: Map<string, number>;
  pubkeyBySlot: Map<number, string>;
  nextSlot: { current: number };
}

function getOrAssignSlot(d: LayoutDispatch, pubkey: string): number {
  const existing = d.slotByPubkey.get(pubkey);
  if (existing !== undefined) return existing;
  const slot = d.nextSlot.current++;
  d.slotByPubkey.set(pubkey, slot);
  d.pubkeyBySlot.set(slot, pubkey);
  return slot;
}

function ensureNode(
  graph: Graph,
  id: string,
  partnerId: string | null,
  components: ComponentState,
  dispatch: LayoutDispatch | null,
): void {
  if (graph.hasNode(id)) return;

  // If this node was already assigned to a component via a prior
  // union, and that component already has enough members placed in
  // the graph, spawn near the component centroid instead of the raw
  // partner position. This avoids orphan-scatter coords (random
  // ±5000) propagating through the giant component when the layout
  // skips pairwise repulsion for large components.
  const compId = components.parent.get(id);
  if (compId !== undefined) {
    const members = components.members.get(compId);
    if (members && members.size > CENTROID_THRESHOLD) {
      let sx = 0, sy = 0, n = 0;
      for (const m of members) {
        if (m === id) continue;
        if (!graph.hasNode(m)) continue;
        sx += graph.getNodeAttribute(m, "x") as number;
        sy += graph.getNodeAttribute(m, "y") as number;
        n++;
      }
      if (n > 0) {
        const cx = sx / n;
        const cy = sy / n;
        const angle = hash01(id) * Math.PI * 2;
        const sxNew = cx + SPAWN_RADIUS * 2 * Math.cos(angle);
        const syNew = cy + SPAWN_RADIUS * 2 * Math.sin(angle);
        addToComponent(components, id);
        graph.addNode(id, {
          x: sxNew,
          y: syNew,
          size: 0.8,
          color: ROLE_PALETTE.normal.rgb,
          label: nodeLabel(id),
          degree: 0,
          solDegree: 0,
          splDegree: 0,
          volume: 0,
          selfLoops: 0,
          inVol: 0,
          outVol: 0,
          role: "normal" as NodeRole,
          bidirVol: 0,
        });
        if (dispatch !== null) {
          const slot = getOrAssignSlot(dispatch, id);
          dispatch.client.addNode(slot, sxNew, syNew);
        }
        return;
      }
    }
  }

  // Fallback: partner-aware placement or orphan scatter.
  const { x, y } = placeNear(graph, id, partnerId);
  addToComponent(components, id);
  graph.addNode(id, {
    x,
    y,
    size: 0.8,
    color: ROLE_PALETTE.normal.rgb,
    label: nodeLabel(id),
    degree: 0,
    solDegree: 0,
    splDegree: 0,
    volume: 0,
    selfLoops: 0,
    inVol: 0,
    outVol: 0,
    role: "normal" as NodeRole,
    bidirVol: 0,
  });
  if (dispatch !== null) {
    const slot = getOrAssignSlot(dispatch, id);
    dispatch.client.addNode(slot, x, y);
  }
}

// SPL/Token-2022 edges arrive with `volume_sol == 0` and `mint`
// set. Every volume increment below uses `e.volume_sol` directly,
// so SPL edges contribute zero to all SOL-denominated signals
// while still bumping `degree`, `txCount`, etc.
function applyEdge(
  graph: Graph,
  e: RawEdge,
  components: ComponentState,
  mintAddrs: Set<string>,
  dispatch: LayoutDispatch | null,
): void {
  if (e.kind === "mint") {
    mintAddrs.add(e.from);
  } else if (e.kind === "burn") {
    mintAddrs.add(e.to);
  }
  if (e.from === e.to) {
    ensureNode(graph, e.from, null, components, dispatch);
    const cur = (graph.getNodeAttribute(e.from, "selfLoops") as number) + 1;
    graph.setNodeAttribute(e.from, "selfLoops", cur);
    graph.setNodeAttribute(e.from, "size", nodeSize(graph, e.from));
    return;
  }

  const fromExists = graph.hasNode(e.from);
  const toExists = graph.hasNode(e.to);
  if (!fromExists && !toExists) {
    ensureNode(graph, e.from, null, components, dispatch);
    ensureNode(graph, e.to, e.from, components, dispatch);
  } else if (!fromExists) {
    ensureNode(graph, e.from, e.to, components, dispatch);
  } else if (!toExists) {
    ensureNode(graph, e.to, e.from, components, dispatch);
  }

  incAttr(graph, e.from, "volume", e.volume_sol);
  incAttr(graph, e.to, "volume", e.volume_sol);
  incAttr(graph, e.from, "outVol", e.volume_sol);
  incAttr(graph, e.to, "inVol", e.volume_sol);

  const isSpl = !!e.mint;
  if (graph.hasEdge(e.from, e.to)) {
    const eid = graph.edge(e.from, e.to)!;
    incAttr(graph, eid, "volume", e.volume_sol, "edge");
    incAttr(graph, eid, "txCount", 1, "edge");
    bumpDirection(graph, eid, e);
    graph.setEdgeAttribute(eid, "weight", graph.getEdgeAttribute(eid, "txCount") as number);
    if (isSpl && !graph.getEdgeAttribute(eid, "hasSpl")) {
      graph.setEdgeAttribute(eid, "hasSpl", true);
      incAttr(graph, e.from, "splDegree", 1);
      incAttr(graph, e.to, "splDegree", 1);
    } else if (!isSpl && !graph.getEdgeAttribute(eid, "hasSol")) {
      graph.setEdgeAttribute(eid, "hasSol", true);
      incAttr(graph, e.from, "solDegree", 1);
      incAttr(graph, e.to, "solDegree", 1);
    }
    if (dispatch !== null) {
      const fs = dispatch.slotByPubkey.get(e.from);
      const ts = dispatch.slotByPubkey.get(e.to);
      if (fs !== undefined && ts !== undefined) {
        dispatch.client.bumpEdgeWeight(fs, ts, 1);
      }
    }
  } else {
    graph.addEdgeWithKey(e.signature, e.from, e.to, {
      volume: e.volume_sol,
      txCount: 1,
      weight: 1,
      canonicalFrom: e.from,
      volAB: e.volume_sol,
      volBA: 0,
      txAB: 1,
      txBA: 0,
      size: EDGE_DEFAULT_SIZE,
      color: e.kind ? colorForEdgeKind(e.kind) : EDGE_COLOR,
      kind: e.kind ?? "transfer",
      hasSol: !isSpl,
      hasSpl: isSpl,
    });
    incAttr(graph, e.from, "degree", 1);
    incAttr(graph, e.to, "degree", 1);
    if (isSpl) {
      incAttr(graph, e.from, "splDegree", 1);
      incAttr(graph, e.to, "splDegree", 1);
    } else {
      incAttr(graph, e.from, "solDegree", 1);
      incAttr(graph, e.to, "solDegree", 1);
    }
    commitEdge(graph, components, e.from, e.to);
    if (dispatch !== null) {
      const fs = dispatch.slotByPubkey.get(e.from);
      const ts = dispatch.slotByPubkey.get(e.to);
      if (fs !== undefined && ts !== undefined) {
        dispatch.client.addEdge(fs, ts, 1);
      }
    }
  }
}

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
    const v = graph.getEdgeAttribute(eid, "volume") as number;
    incAttr(graph, e.from, "bidirVol", v);
    incAttr(graph, e.to, "bidirVol", v);
  } else if (isBidir) {
    incAttr(graph, e.from, "bidirVol", e.volume_sol);
    incAttr(graph, e.to, "bidirVol", e.volume_sol);
  }
}

const EDGE_COLOR = "rgba(200,210,235,0.25)";
// Edge thickness is uniform  volume is expressed through node size
// + color, not edge width. Constant avoids per-edge work that used
// to call a `edgeWidth(vol, graph, from, to)` no-op (the old impl
// always returned 0.6) and a `refreshEdgeSizes` loop on every edge
// add that re-set the same constant on every incident edge.
const EDGE_DEFAULT_SIZE = 0.6;

function commitEdge(
  graph: Graph,
  components: ComponentState,
  fromId: string,
  toId: string,
): void {
  const rootA = findRoot(components, fromId);
  const rootB = findRoot(components, toId);
  if (rootA !== rootB) {
    const merge = union(components, fromId, toId);
    if (merge.merged) {
      const anchor = merge.winner === rootA ? fromId : toId;
      migrateMembersToAnchor(graph, merge.migrated, anchor);
    }
  }
  graph.setNodeAttribute(fromId, "size", nodeSize(graph, fromId));
  graph.setNodeAttribute(toId, "size", nodeSize(graph, toId));
}

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

const NODE_SIZE_MIN_PX = 1.5;
const NODE_SIZE_MAX_PX = 10;
const NODE_SIZE_REF_DEGREE = 60;

function nodeSize(graph: Graph, id: string): number {
  const degree = (graph.getNodeAttribute(id, "degree") as number) ?? 0;
  if (degree <= 1) return 0.8;
  const norm = Math.min(1, (degree - 1) / (NODE_SIZE_REF_DEGREE - 1));
  return NODE_SIZE_MIN_PX + Math.sqrt(norm) * (NODE_SIZE_MAX_PX - NODE_SIZE_MIN_PX);
}

