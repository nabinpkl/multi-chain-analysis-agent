import type Graph from "graphology";

/**
 * Per-node classification derived from the raw graph each detect tick.
 * The point isn't to filter or reduce; it's to attach a label that
 * downstream surfaces (wallet profile, MPC explorer, etc.) can rely on
 * without recomputing. The raw graph stays raw  we just tag it.
 */
export type NodeRole =
  | "token-mint" // Mint pubkey: an SPL/Token-2022 mint account, not a wallet.
  | "tip-account" // Jito-style fee/tip receiver: high degree, dust avg/edge.
  | "mev-searcher" // Touches >=7 tip accounts, near-zero non-tip SOL footprint.
  | "multi-hub" // High-degree wallet touching both SOL and SPL counterparties.
  | "sol-hub" // High-degree wallet whose neighbors are reached only via SOL.
  | "spl-hub" // High-degree wallet whose neighbors are reached only via SPL.
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
   * Set of addresses observed as SPL mint pubkeys (i.e. they appeared
   * as `from` on a `kind="mint"` edge or `to` on a `kind="burn"` edge).
   * Mint pubkeys are token contracts, not user wallets, and must not
   * be classified by the tip/whale/flow-hub heuristics  the high-fanout
   * pattern from a meme-coin launch would otherwise look exactly like
   * a tip account. Override with `token-mint` first, regardless of
   * other signals.
   */
  mintAddrs: Set<string>;
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

// Hub signature: connectivity only, no amount filter. A wallet with
// 50+ unique counterparties is structurally a hub regardless of how
// much value moves through it. The sub-classification (sol-hub /
// spl-hub / multi-hub) uses binary presence of SOL vs SPL neighbors,
// not amounts.
const HUB_DEGREE_MIN = 50;

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
 *   token-mint -> tip-account -> mev-searcher -> multi-hub -> sol-hub
 *   -> spl-hub -> whale -> mpc-member -> normal
 *
 * `token-mint` runs first because mint pubkeys are token contracts and
 * a popular meme-coin mint can rack up thousands of recipient edges
 * with tiny per-edge volume  exactly the tip-account signature.
 * Without the override the classifier would mislabel them.
 *
 * Hub labels (multi/sol/spl) come after the more specific MEV/tip
 * labels but before the value-based whale label. The split between
 * the three hub types is purely on connectivity: does this wallet
 * have any SOL neighbor, any SPL neighbor, both, or only one.
 * No amount thresholds.
 */
export function classifyNodes(input: ClassifyInput): Map<string, NodeRole> {
  const { graph, tipAddrs, mpcMembers, mintAddrs, tipsTouchedByNode } = input;
  const roles = new Map<string, NodeRole>();

  graph.forEachNode((id) => {
    if (mintAddrs.has(id)) {
      roles.set(id, "token-mint");
      return;
    }
    if (tipAddrs.has(id)) {
      roles.set(id, "tip-account");
      return;
    }

    const degree = (graph.getNodeAttribute(id, "degree") as number) ?? 0;
    const solDegree = (graph.getNodeAttribute(id, "solDegree") as number) ?? 0;
    const splDegree = (graph.getNodeAttribute(id, "splDegree") as number) ?? 0;
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

    // Hub labels: connectivity-only. A wallet with 50+ unique
    // counterparties is structurally a hub. Sub-type by binary
    // presence of SOL vs SPL neighbors  no fraction thresholds, no
    // amount filters.
    if (degree >= HUB_DEGREE_MIN) {
      const hasSolNeighbor = solDegree >= 1;
      const hasSplNeighbor = splDegree >= 1;
      if (hasSolNeighbor && hasSplNeighbor) {
        roles.set(id, "multi-hub");
        return;
      }
      if (hasSolNeighbor) {
        roles.set(id, "sol-hub");
        return;
      }
      if (hasSplNeighbor) {
        roles.set(id, "spl-hub");
        return;
      }
    }

    // whale: high SOL volume concentrated in a few edges.
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
