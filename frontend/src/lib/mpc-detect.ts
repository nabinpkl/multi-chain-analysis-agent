import type Graph from "graphology";
import louvain from "graphology-communities-louvain";

// Thresholds are intentionally loose for v0: we're watching a 30s-5min
// streaming window, so bidirectional traffic hasn't had time to pile up.
// Tighten these once the observation window is longer.
const BIDIR_VOL_THRESHOLD = 0.25;
const BALANCE_THRESHOLD = 0.35;
const MIN_DEGREE = 2;
const MIN_CLUSTER_SIZE = 3;
const MIN_INTRA_VOLUME_SHARE = 0.35;
const MIN_LOOPER_SHARE = 0.2;

interface NodeStats {
  looperShare: number;
  intraVolShare: number;
  size: number;
  totalVolume: number;
}

export interface MpcDetection {
  mpcCommunities: Set<number>;
  communityStats: Map<number, NodeStats>;
}

function nodeLooksLikeLooper(
  volume: number,
  inVol: number,
  outVol: number,
  bidirVol: number,
  degree: number,
): boolean {
  if (degree < MIN_DEGREE || volume <= 0) return false;
  const loopRatio = bidirVol / volume;
  const denom = inVol + outVol;
  const balance = denom > 0 ? 1 - Math.abs(inVol - outVol) / denom : 0;
  return loopRatio >= BIDIR_VOL_THRESHOLD && balance >= BALANCE_THRESHOLD;
}

/**
 * Run Louvain on the local graphology instance. Used in `frontend`
 * Louvain mode. In `backend` mode the hook gets `nodeToCommunity`
 * from the SSE `AnalyticsBatch` stream instead and never calls this.
 */
export function runFrontendLouvain(graph: Graph): Map<string, number> {
  const mapping = louvain(graph, { getEdgeWeight: "weight" });
  const out = new Map<string, number>();
  for (const [id, c] of Object.entries(mapping)) out.set(id, c);
  return out;
}

/**
 * MPC scoring: classify communities as "MPC-like" based on bidirectional
 * volume + intra-cluster volume share. Pure scoring step; takes the
 * `nodeToCommunity` map from whatever source (frontend Louvain or
 * backend `AnalyticsBatch`) so it works under either mode.
 */
export function detectMpcClusters(
  graph: Graph,
  nodeToCommunity: Map<string, number>,
): MpcDetection {
  const byCommunity = new Map<number, string[]>();
  for (const [id, c] of nodeToCommunity) {
    const arr = byCommunity.get(c);
    if (arr) arr.push(id);
    else byCommunity.set(c, [id]);
  }

  const communityStats = new Map<number, NodeStats>();
  const mpcCommunities = new Set<number>();

  for (const [c, members] of byCommunity) {
    if (members.length < MIN_CLUSTER_SIZE) continue;

    let loopers = 0;
    let totalVolume = 0;
    for (const id of members) {
      if (!graph.hasNode(id)) continue;
      const vol = graph.getNodeAttribute(id, "volume") as number;
      const inVol = graph.getNodeAttribute(id, "inVol") as number;
      const outVol = graph.getNodeAttribute(id, "outVol") as number;
      const bidir = graph.getNodeAttribute(id, "bidirVol") as number;
      const degree = graph.getNodeAttribute(id, "degree") as number;
      totalVolume += vol;
      if (nodeLooksLikeLooper(vol, inVol, outVol, bidir, degree)) loopers++;
    }

    // Intra-community volume: edge volume where both endpoints are in
    // this community. Compared against the total volume touching any
    // member. High ratio = the cluster is a closed world.
    let intraVol = 0;
    let touchVol = 0;
    const memberSet = new Set(members);
    for (const id of members) {
      if (!graph.hasNode(id)) continue;
      graph.forEachEdge(id, (eid, attrs, src, tgt) => {
        const other = src === id ? tgt : src;
        const v = attrs.volume as number;
        touchVol += v;
        if (memberSet.has(other)) intraVol += v;
      });
    }
    // Every intra-community edge got counted twice above (once per
    // endpoint); halve it. External edges counted once per member
    // endpoint, which is the right denominator (each edge touches the
    // community once).
    intraVol /= 2;

    const looperShare = loopers / members.length;
    const intraVolShare = touchVol > 0 ? intraVol / touchVol : 0;
    communityStats.set(c, {
      looperShare,
      intraVolShare,
      size: members.length,
      totalVolume,
    });

    if (looperShare >= MIN_LOOPER_SHARE && intraVolShare >= MIN_INTRA_VOLUME_SHARE) {
      mpcCommunities.add(c);
    }
  }

  return { mpcCommunities, communityStats };
}
