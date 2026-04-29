//! MPC (multi-party computation / looping cluster) heuristic. Pure
//! scoring step over a partition: decide which Louvain communities
//! look like rotation rings based on per-node bidirectional volume +
//! intra-community volume share.
//!
//! Direct port of `frontend/src/lib/mpc-detect.ts:detectMpcClusters`.
//! Same thresholds, same node-level heuristic. Operates on the
//! `WindowSnapshot` produced by `snapshot.rs` so it shares the single
//! edge walk with Louvain instead of re-walking.
use rustc_hash::{FxHashMap, FxHashSet};

use crate::analytics::snapshot::{NodeStats, WindowSnapshot};
use crate::graph::interner::NodeIdx;

// Thresholds intentionally loose for v0: streaming windows haven't had
// time for bidirectional traffic to pile up. Tighten once the
// observation window is longer. Values mirror the frontend constants.
const BIDIR_VOL_THRESHOLD: f64 = 0.25;
const BALANCE_THRESHOLD: f64 = 0.35;
const MIN_DEGREE: u32 = 2;
const MIN_CLUSTER_SIZE: usize = 3;
const MIN_INTRA_VOLUME_SHARE: f64 = 0.35;
const MIN_LOOPER_SHARE: f64 = 0.2;

/// Per-node looper test: does this node's traffic look like rotation?
/// Round-trip volume share + balance between in and out.
fn node_looks_like_looper(stats: &NodeStats) -> bool {
    if stats.degree < MIN_DEGREE || stats.volume <= 0.0 {
        return false;
    }
    let loop_ratio = stats.bidir_vol / stats.volume;
    let denom = stats.in_vol + stats.out_vol;
    let balance = if denom > 0.0 {
        1.0 - (stats.in_vol - stats.out_vol).abs() / denom
    } else {
        0.0
    };
    loop_ratio >= BIDIR_VOL_THRESHOLD && balance >= BALANCE_THRESHOLD
}

/// Classify communities as MPC-like. Returns the set of community ids
/// (from `node_to_community`) that meet both the looper-share and
/// intra-community-volume-share thresholds.
pub fn detect_mpc_communities(
    snapshot: &WindowSnapshot,
    node_to_community: &FxHashMap<NodeIdx, u32>,
) -> FxHashSet<u32> {
    // Group nodes by community.
    let mut by_community: FxHashMap<u32, Vec<NodeIdx>> = FxHashMap::default();
    for (&node, &cid) in node_to_community {
        by_community.entry(cid).or_default().push(node);
    }

    let mut flagged: FxHashSet<u32> = FxHashSet::default();

    for (cid, members) in &by_community {
        if members.len() < MIN_CLUSTER_SIZE {
            continue;
        }
        let member_set: FxHashSet<NodeIdx> = members.iter().copied().collect();

        // Looper count.
        let mut loopers: u32 = 0;
        for node in members {
            if let Some(stats) = snapshot.node_stats.get(node) {
                if node_looks_like_looper(stats) {
                    loopers = loopers.saturating_add(1);
                }
            }
        }

        // Intra vs touch volume sweep over the community's adjacency.
        // Same accounting as frontend: every intra-community edge gets
        // counted twice (once per endpoint walk), so halve at the end.
        // External edges count once per member endpoint, which is the
        // correct denominator (each external edge touches the community
        // once).
        let mut intra_vol: f64 = 0.0;
        let mut touch_vol: f64 = 0.0;
        for node in members {
            let Some(neighbors) = snapshot.adj.get(node) else {
                continue;
            };
            for (other, &v) in neighbors {
                touch_vol += v;
                if member_set.contains(other) {
                    intra_vol += v;
                }
            }
        }
        intra_vol /= 2.0;

        let looper_share = loopers as f64 / members.len() as f64;
        let intra_vol_share = if touch_vol > 0.0 {
            intra_vol / touch_vol
        } else {
            0.0
        };

        if looper_share >= MIN_LOOPER_SHARE && intra_vol_share >= MIN_INTRA_VOLUME_SHARE {
            flagged.insert(*cid);
        }
    }

    flagged
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analytics::snapshot::WindowSnapshot;

    fn make_stats(
        degree: u32,
        volume: f64,
        in_vol: f64,
        out_vol: f64,
        bidir_vol: f64,
    ) -> NodeStats {
        NodeStats {
            degree,
            volume,
            in_vol,
            out_vol,
            bidir_vol,
            sol_degree: degree,
            spl_degree: 0,
        }
    }

    fn empty_snapshot() -> WindowSnapshot {
        WindowSnapshot {
            adj: FxHashMap::default(),
            components: FxHashMap::default(),
            node_stats: FxHashMap::default(),
            mint_addrs: FxHashSet::default(),
        }
    }

    #[test]
    fn looper_test_requires_min_degree() {
        // Degree below MIN_DEGREE: never a looper regardless of ratios.
        let stats = make_stats(1, 100.0, 50.0, 50.0, 100.0);
        assert!(!node_looks_like_looper(&stats));
    }

    #[test]
    fn looper_test_passes_with_balanced_round_trip() {
        // 50/50 in/out, full round-trip volume.
        let stats = make_stats(2, 100.0, 50.0, 50.0, 80.0);
        assert!(node_looks_like_looper(&stats));
    }

    #[test]
    fn looper_test_fails_with_one_sided_traffic() {
        // All out, no in: balance = 0, fails.
        let stats = make_stats(2, 100.0, 0.0, 100.0, 0.0);
        assert!(!node_looks_like_looper(&stats));
    }

    #[test]
    fn small_community_under_threshold_not_flagged() {
        // 2 members, threshold 3.
        let mut snap = empty_snapshot();
        snap.node_stats.insert(0, make_stats(2, 100.0, 50.0, 50.0, 80.0));
        snap.node_stats.insert(1, make_stats(2, 100.0, 50.0, 50.0, 80.0));
        let mut node_to_community: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        node_to_community.insert(0, 7);
        node_to_community.insert(1, 7);

        let flagged = detect_mpc_communities(&snap, &node_to_community);
        assert!(!flagged.contains(&7));
    }
}
