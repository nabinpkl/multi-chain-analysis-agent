//! Read-side snapshot: take a brief read lock on `GraphState`, walk the
//! per-window edge deque, build undirected adjacency, derive per-node
//! aggregates (degree, volume, in/out/bidir vol, sol/spl degree), and
//! run a small DSU pass to find connected components. The lock is
//! released the instant `snapshot_window` returns; subsequent analytics
//! work happens off-lock against the owned snapshot.
//!
//! Cost target: ~10-15ms at 50k edges (single-threaded by design  rayon
//! is overkill at this scale and would compete with the ingest path
//! that holds the same lock). The per-pair fold runs after the edge
//! walk; both happen under the read lock because the caller passes a
//! borrowed `GraphState`. Once the WindowSnapshot is returned, the
//! caller drops the lock and all subsequent analyses (Louvain, MPC,
//! MEV, role classification) read only from the owned snapshot.
use rustc_hash::{FxHashMap, FxHashSet};

use crate::graph::GraphState;
use crate::graph::delta::EdgeKind;
use crate::graph::interner::NodeIdx;

/// Per-window adjacency, components, and per-node aggregates. Component
/// ids here are per-window synthetic u32 values (the smallest NodeIdx
/// in each component), independent of `GraphState`'s u64 ComponentId.
pub struct WindowSnapshot {
    pub adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>>,
    pub components: FxHashMap<u32, FxHashSet<NodeIdx>>,
    pub node_stats: FxHashMap<NodeIdx, NodeStats>,
    /// Nodes observed as `src` on a `Mint` edge or `dst` on a `Burn`
    /// edge in this window. Mirrors the frontend's `mintAddrsRef`
    /// populator; classification's first-pass override flags these as
    /// `token-mint` regardless of other signals so a meme-coin launch
    /// doesn't get mislabeled as a tip account.
    pub mint_addrs: FxHashSet<NodeIdx>,
}

/// Per-node aggregates derived from the window's edges. Volume-bearing
/// fields (`volume`, `in_vol`, `out_vol`, `bidir_vol`) count transfer
/// edges only (kind == None); SPL movements don't carry SOL amounts in
/// this v0 ingester. `sol_degree` / `spl_degree` count unique
/// counterparties partitioned by edge kind.
#[derive(Default, Clone, Copy, Debug)]
pub struct NodeStats {
    /// Unique counterparties (any kind).
    pub degree: u32,
    /// Total volume across all transfer edges touching this node.
    pub volume: f64,
    /// Volume on transfer edges where this node is `dst`.
    pub in_vol: f64,
    /// Volume on transfer edges where this node is `src`.
    pub out_vol: f64,
    /// Per pair containing this node, `2 * min(forward, backward)` on
    /// transfer volume. Captures the "round-tripping" component of the
    /// node's traffic. `bidir_vol / volume` is the looper ratio used by
    /// MPC detection.
    pub bidir_vol: f64,
    /// Unique counterparties reached via transfer edges.
    pub sol_degree: u32,
    /// Unique counterparties reached via mint/burn edges.
    pub spl_degree: u32,
}

/// Per-pair accumulator used during the snapshot walk. Folded into
/// `NodeStats` after the walk completes.
#[derive(Default)]
struct PairStats {
    /// Sum of transfer-edge amounts in the canonical (a, b) direction
    /// where a < b.
    transfer_a_to_b: f64,
    /// Sum of transfer-edge amounts in the reverse (b, a) direction.
    transfer_b_to_a: f64,
    has_transfer: bool,
    has_spl: bool,
}

/// Snapshot the per-window edge view of `g`. Builds undirected
/// adjacency, per-pair stats, mint-address set, and DSU components in
/// a single walk. Cost dominated by the edge walk (~5-10ms at 50k);
/// the per-pair fold adds ~2-5ms.
pub fn snapshot_window(g: &GraphState, window_idx: usize) -> WindowSnapshot {
    let mut adj: FxHashMap<NodeIdx, FxHashMap<NodeIdx, f64>> = FxHashMap::default();
    let mut dsu = Dsu::default();
    let mut pair_stats: FxHashMap<(NodeIdx, NodeIdx), PairStats> = FxHashMap::default();
    let mut mint_addrs: FxHashSet<NodeIdx> = FxHashSet::default();

    for (src, dst, amount, kind) in g.iter_window_edges(window_idx) {
        let w = amount as f64;
        // Undirected adjacency: add both directions, summing for parallel /
        // reciprocal edges between the same pair.
        *adj.entry(src).or_default().entry(dst).or_insert(0.0) += w;
        if src != dst {
            *adj.entry(dst).or_default().entry(src).or_insert(0.0) += w;
        }
        dsu.union(src, dst);

        // Mint-address detection: src on Mint, dst on Burn. Mirrors the
        // frontend's `applyEdge` populator so the role classifier's
        // token-mint override fires identically here.
        match kind.as_ref() {
            Some(EdgeKind::Mint) => {
                mint_addrs.insert(src);
            }
            Some(EdgeKind::Burn) => {
                mint_addrs.insert(dst);
            }
            None => {}
        }

        if src == dst {
            // Self-loop: doesn't contribute to pair stats (degree
            // counts unique counterparties, and bidir requires two
            // different endpoints). Adjacency already captured it
            // above for Louvain edge-weight purposes.
            continue;
        }

        // Canonical pair key (smaller idx first) so reverse edges
        // collapse onto the same accumulator.
        let (lo, hi) = if src < dst { (src, dst) } else { (dst, src) };
        let entry = pair_stats.entry((lo, hi)).or_default();
        match kind {
            Some(_) => {
                entry.has_spl = true;
            }
            None => {
                entry.has_transfer = true;
                if src == lo {
                    entry.transfer_a_to_b += w;
                } else {
                    entry.transfer_b_to_a += w;
                }
            }
        }
    }

    // Component id = smallest NodeIdx in the component (deterministic).
    let mut components: FxHashMap<u32, FxHashSet<NodeIdx>> = FxHashMap::default();
    for &node in adj.keys() {
        let root = dsu.find(node);
        components.entry(root).or_default().insert(node);
    }

    // Fold per-pair accumulators into per-node NodeStats. Each pair
    // contributes to both endpoints' degree / kind-degree and to the
    // volume / direction tallies on the transfer side.
    let mut node_stats: FxHashMap<NodeIdx, NodeStats> = FxHashMap::default();
    for (&(a, b), stats) in &pair_stats {
        let entry_a = node_stats.entry(a).or_default();
        entry_a.degree = entry_a.degree.saturating_add(1);
        if stats.has_transfer {
            entry_a.sol_degree = entry_a.sol_degree.saturating_add(1);
            // a -> b is `out` for a, `in` for the partner; b -> a is `in` for a.
            entry_a.out_vol += stats.transfer_a_to_b;
            entry_a.in_vol += stats.transfer_b_to_a;
            entry_a.volume += stats.transfer_a_to_b + stats.transfer_b_to_a;
            let bidir = 2.0 * stats.transfer_a_to_b.min(stats.transfer_b_to_a);
            entry_a.bidir_vol += bidir;
        }
        if stats.has_spl {
            entry_a.spl_degree = entry_a.spl_degree.saturating_add(1);
        }

        let entry_b = node_stats.entry(b).or_default();
        entry_b.degree = entry_b.degree.saturating_add(1);
        if stats.has_transfer {
            entry_b.sol_degree = entry_b.sol_degree.saturating_add(1);
            entry_b.out_vol += stats.transfer_b_to_a;
            entry_b.in_vol += stats.transfer_a_to_b;
            entry_b.volume += stats.transfer_a_to_b + stats.transfer_b_to_a;
            let bidir = 2.0 * stats.transfer_a_to_b.min(stats.transfer_b_to_a);
            entry_b.bidir_vol += bidir;
        }
        if stats.has_spl {
            entry_b.spl_degree = entry_b.spl_degree.saturating_add(1);
        }
    }

    WindowSnapshot {
        adj,
        components,
        node_stats,
        mint_addrs,
    }
}

/// Plain DSU over NodeIdx. Stored as a hashmap because NodeIdx values
/// are sparse for short windows (a 10s window touches a tiny slice of
/// the global interner). Path compression on `find`, union-by-rank on
/// `union`, root chosen as the smaller NodeIdx for deterministic
/// component ids.
#[derive(Default)]
struct Dsu {
    parent: FxHashMap<NodeIdx, NodeIdx>,
    rank: FxHashMap<NodeIdx, u32>,
}

impl Dsu {
    fn find(&mut self, x: NodeIdx) -> NodeIdx {
        let p = *self.parent.entry(x).or_insert(x);
        if p == x {
            return x;
        }
        let root = self.find(p);
        self.parent.insert(x, root);
        root
    }

    fn union(&mut self, a: NodeIdx, b: NodeIdx) {
        let ra = self.find(a);
        let rb = self.find(b);
        if ra == rb {
            return;
        }
        let rank_a = *self.rank.entry(ra).or_insert(0);
        let rank_b = *self.rank.entry(rb).or_insert(0);
        // Bias toward smaller NodeIdx as the root so component id is
        // stable across snapshots when membership doesn't change.
        let (root, child) = if rank_a > rank_b {
            (ra, rb)
        } else if rank_b > rank_a {
            (rb, ra)
        } else if ra <= rb {
            (ra, rb)
        } else {
            (rb, ra)
        };
        self.parent.insert(child, root);
        if rank_a == rank_b {
            self.rank.insert(root, rank_a + 1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Edge;

    fn make_edge(from: &str, to: &str, slot: u64, block_time: u64) -> Edge {
        Edge {
            signature: format!("sig_{from}_{to}_{slot}"),
            instruction_idx: 0,
            slot,
            block_time: block_time as u32,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: String::new(),
            kind: String::new(),
            version: 1,
        }
    }

    fn make_kind_edge(
        from: &str,
        to: &str,
        slot: u64,
        block_time: u64,
        kind: &str,
    ) -> Edge {
        Edge {
            signature: format!("sig_{from}_{to}_{slot}"),
            instruction_idx: 0,
            slot,
            block_time: block_time as u32,
            from_wallet: from.to_string(),
            to_wallet: to.to_string(),
            amount: 1_000_000,
            mint: "MINT_X".to_string(),
            kind: kind.to_string(),
            version: 1,
        }
    }

    #[test]
    fn snapshot_disconnected_pair_yields_two_components() {
        let mut gs = GraphState::default();
        gs.ingest(&make_edge("AAA", "BBB", 1, 1000));
        gs.ingest(&make_edge("CCC", "DDD", 2, 1001));

        let snap = snapshot_window(&gs, 5);
        assert_eq!(snap.components.len(), 2);
        let total_nodes: usize = snap.components.values().map(|s| s.len()).sum();
        assert_eq!(total_nodes, 4);
    }

    #[test]
    fn snapshot_connected_triangle_yields_one_component() {
        let mut gs = GraphState::default();
        gs.ingest(&make_edge("AAA", "BBB", 1, 1000));
        gs.ingest(&make_edge("BBB", "CCC", 2, 1001));
        gs.ingest(&make_edge("CCC", "AAA", 3, 1002));

        let snap = snapshot_window(&gs, 5);
        assert_eq!(snap.components.len(), 1);
    }

    #[test]
    fn node_stats_transfer_pair_one_direction() {
        let mut gs = GraphState::default();
        // A -> B at amount 1_000_000 (1 SOL in lamports).
        gs.ingest(&make_edge("AAA", "BBB", 1, 1000));
        let snap = snapshot_window(&gs, 5);

        let a_idx = snap
            .node_stats
            .keys()
            .find(|&&k| {
                gs.lookup_pubkey(k).map(|p| p == "AAA").unwrap_or(false)
            })
            .copied()
            .unwrap();
        let b_idx = snap
            .node_stats
            .keys()
            .find(|&&k| {
                gs.lookup_pubkey(k).map(|p| p == "BBB").unwrap_or(false)
            })
            .copied()
            .unwrap();

        let a_stats = &snap.node_stats[&a_idx];
        assert_eq!(a_stats.degree, 1);
        assert_eq!(a_stats.sol_degree, 1);
        assert_eq!(a_stats.spl_degree, 0);
        assert!((a_stats.out_vol - 1_000_000.0).abs() < 1e-6);
        assert!((a_stats.in_vol - 0.0).abs() < 1e-6);
        assert!((a_stats.bidir_vol - 0.0).abs() < 1e-6);

        let b_stats = &snap.node_stats[&b_idx];
        assert!((b_stats.in_vol - 1_000_000.0).abs() < 1e-6);
        assert!((b_stats.out_vol - 0.0).abs() < 1e-6);
        assert!((b_stats.bidir_vol - 0.0).abs() < 1e-6);
    }

    #[test]
    fn node_stats_bidirectional_pair_credits_min() {
        let mut gs = GraphState::default();
        // A -> B amount 1_000_000, B -> A amount 600_000.
        gs.ingest(&make_edge("AAA", "BBB", 1, 1000));
        let mut e2 = make_edge("BBB", "AAA", 2, 1001);
        e2.amount = 600_000;
        gs.ingest(&e2);

        let snap = snapshot_window(&gs, 5);
        let a_idx = snap
            .node_stats
            .keys()
            .find(|&&k| {
                gs.lookup_pubkey(k).map(|p| p == "AAA").unwrap_or(false)
            })
            .copied()
            .unwrap();
        let stats = &snap.node_stats[&a_idx];
        // bidir = 2 * min(1_000_000, 600_000) = 1_200_000
        assert!((stats.bidir_vol - 1_200_000.0).abs() < 1e-6);
        assert_eq!(stats.degree, 1);
        assert_eq!(stats.sol_degree, 1);
        // volume = total transfer touching node = 1_000_000 + 600_000
        assert!((stats.volume - 1_600_000.0).abs() < 1e-6);
    }

    #[test]
    fn mint_addrs_picks_up_mint_src_and_burn_dst() {
        let mut gs = GraphState::default();
        // Mint edge: MINT_AUTH -> RECEIVER. Mint authority is src,
        // mirrors frontend: `e.kind === "mint"` -> mintAddrs.add(src).
        gs.ingest(&make_kind_edge("MINTAUTH", "RECV", 1, 1000, "mint"));
        // Burn edge: HOLDER -> MINT_AUTH. Mint authority is dst.
        gs.ingest(&make_kind_edge("HOLDER", "MINTAUTH2", 2, 1001, "burn"));

        let snap = snapshot_window(&gs, 5);
        let pubkeys: FxHashSet<&str> = snap
            .mint_addrs
            .iter()
            .map(|&idx| gs.lookup_pubkey(idx).unwrap())
            .collect();
        assert!(pubkeys.contains("MINTAUTH"));
        assert!(pubkeys.contains("MINTAUTH2"));
        // Holders / receivers do NOT get flagged.
        assert!(!pubkeys.contains("RECV"));
        assert!(!pubkeys.contains("HOLDER"));
    }
}
