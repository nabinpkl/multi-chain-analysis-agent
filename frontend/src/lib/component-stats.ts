import type Graph from "graphology";
import type { ComponentState } from "@/lib/components";
import type { NodeRole } from "@/lib/role-detect";

/**
 * Per-connected-component aggregates derived from the raw graph. Lives
 * in a sidecar map (not on the graph) because components are an index
 * over nodes, not entities themselves. Computed once per detect tick;
 * downstream UIs read this without recomputing.
 */
export interface ComponentStats {
  rootId: string;
  size: number;
  /**
   * Total SOL flowing inside this component. Each node contributes
   * inVol + outVol, halved because every intra-component edge is
   * counted twice (once per endpoint). Cross-component edges are
   * impossible by definition of a connected component, so this is
   * exact for the visible-edge subgraph.
   */
  totalVolume: number;
  edgeCount: number;
  topByDegreeId: string;
  topByDegreeValue: number;
  topByVolumeId: string;
  topByVolumeValue: number;
  /** How many nodes of each role live in this component. */
  roleCounts: Record<NodeRole, number>;
}

const EMPTY_ROLE_COUNTS = (): Record<NodeRole, number> => ({
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

export function computeComponentStats(
  graph: Graph,
  components: ComponentState,
  roles: Map<string, NodeRole>,
): Map<string, ComponentStats> {
  const out = new Map<string, ComponentStats>();

  for (const [rootId, members] of components.members) {
    let totalSumOfNodeVolumes = 0;
    let edgeCountTimesTwo = 0;
    let topByDegreeId = "";
    let topByDegreeValue = -1;
    let topByVolumeId = "";
    let topByVolumeValue = -1;
    const roleCounts = EMPTY_ROLE_COUNTS();

    for (const id of members) {
      const inVol = (graph.getNodeAttribute(id, "inVol") as number) ?? 0;
      const outVol = (graph.getNodeAttribute(id, "outVol") as number) ?? 0;
      const volume = (graph.getNodeAttribute(id, "volume") as number) ?? 0;
      const degree = (graph.getNodeAttribute(id, "degree") as number) ?? 0;

      totalSumOfNodeVolumes += inVol + outVol;
      edgeCountTimesTwo += degree;

      if (degree > topByDegreeValue) {
        topByDegreeValue = degree;
        topByDegreeId = id;
      }
      if (volume > topByVolumeValue) {
        topByVolumeValue = volume;
        topByVolumeId = id;
      }

      const role = roles.get(id) ?? "normal";
      roleCounts[role] += 1;
    }

    out.set(rootId, {
      rootId,
      size: members.size,
      // Each intra-component edge is counted once per endpoint in the
      // sum, so halving recovers the true total. Same trick the MPC
      // detector uses.
      totalVolume: totalSumOfNodeVolumes / 2,
      edgeCount: edgeCountTimesTwo / 2,
      topByDegreeId,
      topByDegreeValue: Math.max(0, topByDegreeValue),
      topByVolumeId,
      topByVolumeValue: Math.max(0, topByVolumeValue),
      roleCounts,
    });
  }

  return out;
}
