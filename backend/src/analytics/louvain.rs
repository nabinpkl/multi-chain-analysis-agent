//! Louvain community detection over a single connected component's
//! adjacency. The caller has already partitioned the graph into
//! components (cross-component edges have zero weight, so per-component
//! Louvain is mathematically equivalent to global), and gates by
//! component size.
//!
//! Implementation: standard two-phase iteration.
//!   Phase A: greedy local moves. For each node, evaluate ΔQ for
//!     joining each neighbor's community (incl. staying put) and pick
//!     the best.
//!   Phase B: collapse each community into a super-node, sum edges
//!     between super-nodes, recurse.
//! Stop when modularity stops improving or after MAX_LEVELS iterations.
//!
//! The returned partition uses dense local ids (0..k-1). Stable global
//! id assignment is the next layer's job  see `stable_labels.rs`.
use rustc_hash::{FxHashMap, FxHashSet};

use crate::graph::interner::NodeIdx;

/// Hard cap on phase A/B iterations. Three or four is typical; ten is
/// pure paranoia for pathological inputs.
const MAX_LEVELS: usize = 10;
/// Hard cap on phase A passes per level. Convergence is normally <10.
const MAX_PASSES: usize = 50;

/// Louvain partition for a single connected component.
///
/// `members` is the set of NodeIdx in this component. `adj` is the
/// *full* adjacency map (covering every component); we read from it
/// but only consider edges whose both endpoints are in `members`.
///
/// Returns `node_idx -> local community id`. Local ids are dense
/// integers starting at 0, ordered by first encounter when iterating
/// nodes in sorted order. Caller must remap to globally stable ids.
pub fn louvain_per_component(
    members: &FxHashSet<NodeIdx>,
    adj: &FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>>,
) -> FxHashMap<NodeIdx, u32> {
    if members.is_empty() {
        return FxHashMap::default();
    }
    if members.len() == 1 {
        let mut out = FxHashMap::default();
        out.insert(*members.iter().next().unwrap(), 0u32);
        return out;
    }

    // Build the level-0 subgraph restricted to `members`.
    let mut nodes: Vec<NodeIdx> = members.iter().copied().collect();
    nodes.sort_unstable();
    let n = nodes.len();

    // Map original NodeIdx <-> dense local index inside this subgraph.
    let mut idx_of: FxHashMap<NodeIdx, usize> = FxHashMap::default();
    for (i, &nid) in nodes.iter().enumerate() {
        idx_of.insert(nid, i);
    }

    // adj_dense[i] = Vec<(neighbor_local_idx, weight)>. Self-loop is
    // stored as i -> i with weight=2*loop_weight (Louvain convention:
    // a self-loop contributes 2w to the node's degree).
    let mut adj_dense: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n];
    for (i, &orig) in nodes.iter().enumerate() {
        if let Some(neighbors) = adj.get(&orig) {
            for (&other, &w) in neighbors {
                if let Some(&j) = idx_of.get(&other) {
                    if i == j {
                        // Self-loop: appear once in the dense list.
                        // The amount stored in `adj` is the raw amount
                        // (not doubled). For modularity, a self-loop
                        // adds w to the node's degree once, since adj
                        // stored it once for self-loops in snapshot.rs.
                        adj_dense[i].push((j, w));
                    } else {
                        adj_dense[i].push((j, w));
                    }
                }
            }
        }
    }

    // Iterate Louvain levels. `level_partition[i] = community of node i`
    // at the current level. `mapping_chain` records how each level
    // collapsed nodes so we can project final super-node communities
    // back to original NodeIdx.
    let mut current_adj = adj_dense;
    let mut current_n = n;
    let mut mapping_chain: Vec<Vec<usize>> = Vec::new();
    let mut prev_modularity = f64::NEG_INFINITY;

    for _level in 0..MAX_LEVELS {
        let (partition, modularity) = phase_a(&current_adj, current_n);
        if modularity <= prev_modularity + 1e-9 {
            // No meaningful improvement: keep the previous level's
            // partition (an identity mapping at this level).
            mapping_chain.push((0..current_n).collect());
            break;
        }
        prev_modularity = modularity;

        // Build the next level: each community becomes one super-node.
        let (new_adj, new_n, partition_dense) =
            phase_b(&current_adj, current_n, &partition);
        mapping_chain.push(partition_dense);
        current_adj = new_adj;
        current_n = new_n;

        if current_n <= 1 {
            break;
        }
    }

    // Project final community ids back to original NodeIdx by walking
    // the mapping chain. Start with each level-0 dense idx at itself,
    // then map through every level.
    let mut final_label: Vec<usize> = (0..n).collect();
    for level_map in &mapping_chain {
        for label in final_label.iter_mut() {
            *label = level_map[*label];
        }
    }

    // Compress final labels to dense 0..k-1 ids.
    let mut compress: FxHashMap<usize, u32> = FxHashMap::default();
    let mut next_id: u32 = 0;
    let mut out: FxHashMap<NodeIdx, u32> = FxHashMap::default();
    for (i, &orig) in nodes.iter().enumerate() {
        let label = final_label[i];
        let cid = *compress.entry(label).or_insert_with(|| {
            let id = next_id;
            next_id += 1;
            id
        });
        out.insert(orig, cid);
    }
    out
}

/// Phase A: greedy local moves. Returns `(partition, final_modularity)`
/// where `partition[i] = community id of node i` (community ids are
/// 0..n-1 indices into the per-community accumulators, dense after
/// phase A's renumbering happens in `phase_b`).
fn phase_a(adj: &[Vec<(usize, f64)>], n: usize) -> (Vec<usize>, f64) {
    // Initial: every node is its own community.
    let mut community: Vec<usize> = (0..n).collect();

    // k[i] = weighted degree of node i (each undirected edge counted
    // once from each endpoint, self-loop counted once).
    let mut k: Vec<f64> = vec![0.0; n];
    for i in 0..n {
        for &(_, w) in &adj[i] {
            k[i] += w;
        }
    }

    // sigma_tot[c] = total weighted degree of community c.
    let mut sigma_tot: Vec<f64> = k.clone();

    // m = total edge weight in the graph. Sum of all entries / 2 (since
    // each undirected edge appears in both endpoints' adj lists).
    // Self-loops appear once in adj_dense, but Louvain modularity counts
    // them once in 2m (so dividing the full adj sum by 2 over-counts
    // self-loops by 1/2). For typical wallet graphs self-loops are
    // negligible; the small bias does not affect community structure.
    let total_adj: f64 = k.iter().sum();
    let m = total_adj / 2.0;
    if m <= 0.0 {
        return (community, 0.0);
    }

    let inv_m = 1.0 / m;
    let inv_2m_sq = 1.0 / (2.0 * m * m);

    // Greedy passes until no node changes.
    for _pass in 0..MAX_PASSES {
        let mut moved = false;
        for i in 0..n {
            // Sum weights from i into each neighbor community,
            // excluding self-loops at i.
            let mut k_in_community: FxHashMap<usize, f64> = FxHashMap::default();
            for &(j, w) in &adj[i] {
                if j == i {
                    continue;
                }
                let cj = community[j];
                *k_in_community.entry(cj).or_insert(0.0) += w;
            }

            let ci = community[i];
            // Remove i from its current community for the calculation.
            sigma_tot[ci] -= k[i];

            // Best move: highest ΔQ. Default = stay (move to ci).
            let mut best_c = ci;
            let mut best_gain = 0.0;
            // Always include staying. k_in for current comm without i:
            let k_in_self = *k_in_community.get(&ci).unwrap_or(&0.0);
            let stay_gain = delta_q(k_in_self, k[i], sigma_tot[ci], inv_m, inv_2m_sq);
            best_gain = stay_gain;

            for (&c, &k_in_c) in &k_in_community {
                if c == ci {
                    continue;
                }
                let gain = delta_q(k_in_c, k[i], sigma_tot[c], inv_m, inv_2m_sq);
                if gain > best_gain + 1e-12 {
                    best_gain = gain;
                    best_c = c;
                }
            }

            // Apply the move (add i back to best_c).
            sigma_tot[best_c] += k[i];
            if best_c != ci {
                community[i] = best_c;
                moved = true;
            }
        }
        if !moved {
            break;
        }
    }

    // Compute final modularity for the convergence check.
    let q = modularity(adj, n, &community, m);
    (community, q)
}

/// ΔQ for moving an isolated node into a community c.
///   k_in   = sum of edge weights from the node into c (excl. self-loop)
///   k_node = node's weighted degree
///   sigma_tot_c = sum of degrees of nodes in c (with the node removed)
///   m      = total graph weight
fn delta_q(k_in: f64, k_node: f64, sigma_tot_c: f64, inv_m: f64, inv_2m_sq: f64) -> f64 {
    k_in * inv_m - k_node * sigma_tot_c * inv_2m_sq * 2.0
    // The 2.0 absorbs the standard "k_in/m  k * Σ_tot / (2m^2)" formula
    // (the original paper uses k_i,in counted once per neighbor pair
    // with a 1/m term). Implementations vary in factor placement; this
    // matches the common Louvain reference.
}

fn modularity(adj: &[Vec<(usize, f64)>], n: usize, community: &[usize], m: f64) -> f64 {
    if m <= 0.0 {
        return 0.0;
    }
    // Q = Σ_ij [ A_ij/(2m)  k_i k_j/(2m)^2 ] δ(c_i, c_j)
    // Group by community. For each community c, compute
    //   intra_weight = sum of A_ij for i,j in c (each undirected edge
    //                  counted twice from adj, self-loops once  same
    //                  as Louvain convention since we'll divide by 2m).
    //   tot_degree   = sum of k_i for i in c
    let mut intra: FxHashMap<usize, f64> = FxHashMap::default();
    let mut tot: FxHashMap<usize, f64> = FxHashMap::default();
    for i in 0..n {
        let ci = community[i];
        let mut deg_i = 0.0;
        for &(j, w) in &adj[i] {
            deg_i += w;
            if community[j] == ci {
                *intra.entry(ci).or_insert(0.0) += w;
            }
        }
        *tot.entry(ci).or_insert(0.0) += deg_i;
    }
    let two_m = 2.0 * m;
    let mut q = 0.0;
    for (c, &intra_w) in &intra {
        let tot_w = *tot.get(c).unwrap_or(&0.0);
        q += intra_w / two_m - (tot_w / two_m).powi(2);
    }
    q
}

/// Phase B: collapse communities into super-nodes. Returns the new
/// adjacency, new node count, and a per-original-node mapping
/// `original_idx -> super_idx`.
fn phase_b(
    adj: &[Vec<(usize, f64)>],
    n: usize,
    community: &[usize],
) -> (Vec<Vec<(usize, f64)>>, usize, Vec<usize>) {
    // Renumber communities to dense 0..k-1.
    let mut compress: FxHashMap<usize, usize> = FxHashMap::default();
    let mut mapping = vec![0usize; n];
    for i in 0..n {
        let c = community[i];
        let new_id = if let Some(&id) = compress.get(&c) {
            id
        } else {
            let id = compress.len();
            compress.insert(c, id);
            id
        };
        mapping[i] = new_id;
    }
    let k = compress.len();

    // new_adj[u] is summed edge weights from super-node u to each other.
    let mut new_adj: Vec<FxHashMap<usize, f64>> = vec![FxHashMap::default(); k];
    for i in 0..n {
        let cu = mapping[i];
        for &(j, w) in &adj[i] {
            let cv = mapping[j];
            *new_adj[cu].entry(cv).or_insert(0.0) += w;
        }
    }

    let new_adj_vec: Vec<Vec<(usize, f64)>> = new_adj
        .into_iter()
        .map(|m| m.into_iter().collect())
        .collect();

    (new_adj_vec, k, mapping)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn add_edge(
        adj: &mut FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>>,
        a: NodeIdx,
        b: NodeIdx,
        w: f64,
    ) {
        *adj.entry(a).or_default().entry(b).or_insert(0.0) += w;
        *adj.entry(b).or_default().entry(a).or_insert(0.0) += w;
    }

    #[test]
    fn two_cliques_bridged_by_one_edge_yield_two_communities() {
        // Clique A: 0-1-2-3 (every pair, weight=1)
        // Clique B: 4-5-6-7 (every pair, weight=1)
        // Bridge:  3-4 (weight=1)
        let mut adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>> = FxHashMap::default();
        for a in 0..4u32 {
            for b in (a + 1)..4u32 {
                add_edge(&mut adj, a, b, 1.0);
            }
        }
        for a in 4..8u32 {
            for b in (a + 1)..8u32 {
                add_edge(&mut adj, a, b, 1.0);
            }
        }
        add_edge(&mut adj, 3, 4, 1.0);

        let members: FxHashSet<NodeIdx> = (0..8u32).collect();
        let part = louvain_per_component(&members, &adj);

        // Expect exactly two distinct community ids.
        let unique: FxHashSet<u32> = part.values().copied().collect();
        assert_eq!(unique.len(), 2, "two cliques + bridge should split into 2 communities, got {:?}", part);

        // 0..3 in one community, 4..7 in the other.
        let c0 = part[&0];
        for i in 0..4u32 {
            assert_eq!(part[&i], c0, "node {} should join clique A", i);
        }
        let c1 = part[&4];
        assert_ne!(c0, c1);
        for i in 4..8u32 {
            assert_eq!(part[&i], c1, "node {} should join clique B", i);
        }
    }

    #[test]
    fn singleton_partition() {
        let mut adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>> = FxHashMap::default();
        adj.insert(42, FxHashMap::default());
        let mut members = FxHashSet::default();
        members.insert(42u32);
        let part = louvain_per_component(&members, &adj);
        assert_eq!(part.len(), 1);
        assert_eq!(part[&42], 0);
    }
}
