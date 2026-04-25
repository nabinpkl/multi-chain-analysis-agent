import type Graph from "graphology";

/**
 * Per-node classification derived from the raw graph each detect tick.
 * The point isn't to filter or reduce; it's to attach a label that
 * downstream surfaces (wallet profile, MPC explorer, etc.) can rely on
 * without recomputing. The raw graph stays raw  we just tag it.
 */
export type NodeRole =
  | "tip-account" // Jito-style fee/tip receiver: high degree, dust avg/edge.
  | "mev-searcher" // Touches >=7 tip accounts, near-zero non-tip SOL footprint.
  | "flow-hub" // High-degree real economic actor (DEX vault, exchange hot wallet).
  | "whale" // High-volume, low-fanout wallet (OTC, big trader).
  | "mpc-member" // Member of a Louvain community flagged by the MPC heuristic.
  | "normal";

export interface ClassifyInput {
  graph: Graph;
  /** Auto-detected tip-style addresses, from the existing tip detector. */
  tipAddrs: Set<string>;
  /** Union of all flagged MPC community member ids. */
  mpcMembers: Set<string>;
  /**
   * Optional precomputed map of "how many tip accounts does this node
   * touch?" If provided, we skip a second neighbor walk for the
   * mev-searcher check. Computed by the existing tip-style profiling
   * pass in use-raw-stream.ts.
   */
  tipsTouchedByNode?: Map<string, number>;
}

// MEV searcher signature thresholds. Heavy bots paying every shift have
// 7-8 tip-account touches and effectively zero non-tip SOL flow because
// their profits are SPL-token denominated and invisible to our parser.
const MEV_TIPS_TOUCHED_MIN = 7;
const MEV_MAX_SOL_FOOTPRINT = 0.01;

// Flow-hub signature: real economic concentration. Distinct from tips
// (which have orders of magnitude smaller per-edge volume).
const FLOW_HUB_DEGREE_MIN = 50;
const FLOW_HUB_AVG_VOL_PER_EDGE_MIN = 0.05;

// Whale signature: a few big counterparties. In contrast to flow-hub
// (many edges, big average), the whale concentrates volume on a
// handful of edges  classic OTC pattern.
const WHALE_VOLUME_MIN = 100;
const WHALE_DEGREE_MAX = 10;

/**
 * Classify every node in the graph. First pass is essentially a
 * dictionary lookup against the tip set; subsequent passes read node
 * attributes (degree, volume, in/out totals) plus optionally the
 * tipsTouched map. Returns a Map keyed by node id; ids not in the map
 * have role "normal" by definition.
 *
 * Resolution order is "first match wins" so a wallet that's both an
 * MPC member and a whale gets the more specific tag (whale). Order:
 *   tip-account -> mev-searcher -> flow-hub -> whale -> mpc-member -> normal
 */
export function classifyNodes(input: ClassifyInput): Map<string, NodeRole> {
  const { graph, tipAddrs, mpcMembers, tipsTouchedByNode } = input;
  const roles = new Map<string, NodeRole>();

  graph.forEachNode((id) => {
    if (tipAddrs.has(id)) {
      roles.set(id, "tip-account");
      return;
    }

    const degree = (graph.getNodeAttribute(id, "degree") as number) ?? 0;
    const volume = (graph.getNodeAttribute(id, "volume") as number) ?? 0;
    const inVol = (graph.getNodeAttribute(id, "inVol") as number) ?? 0;
    const outVol = (graph.getNodeAttribute(id, "outVol") as number) ?? 0;

    // mev-searcher: touches enough tip accounts AND has near-zero
    // non-tip SOL footprint. Use the precomputed map if available,
    // otherwise count tip-account neighbors directly.
    let tipsTouched = tipsTouchedByNode?.get(id);
    if (tipsTouched === undefined) {
      tipsTouched = 0;
      graph.forEachNeighbor(id, (other) => {
        if (tipAddrs.has(other)) tipsTouched! += 1;
      });
    }
    if (
      tipsTouched >= MEV_TIPS_TOUCHED_MIN &&
      inVol + outVol < MEV_MAX_SOL_FOOTPRINT
    ) {
      roles.set(id, "mev-searcher");
      return;
    }

    // flow-hub: real economic hub. Tip-accounts are excluded by the
    // earlier branch, so no need to re-check here.
    if (degree >= FLOW_HUB_DEGREE_MIN) {
      const avgPerEdge = degree > 0 ? volume / degree : 0;
      if (avgPerEdge >= FLOW_HUB_AVG_VOL_PER_EDGE_MIN) {
        roles.set(id, "flow-hub");
        return;
      }
    }

    // whale: high volume concentrated in a few edges.
    if (volume >= WHALE_VOLUME_MIN && degree <= WHALE_DEGREE_MAX) {
      roles.set(id, "whale");
      return;
    }

    if (mpcMembers.has(id)) {
      roles.set(id, "mpc-member");
      return;
    }

    roles.set(id, "normal");
  });

  return roles;
}
