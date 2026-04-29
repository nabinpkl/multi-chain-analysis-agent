//! Tip-account detection + MEV searcher profiling.
//!
//! Direct port of `frontend/src/hooks/use-raw-stream.ts:581-649`. Tip
//! accounts are high-degree, dust-per-edge wallets (the Jito-style fee
//! receivers). Once we have the tip set, we walk each tip's neighbors
//! to count how many tip accounts each non-tip wallet touches; nodes
//! that touch many tips are MEV searcher candidates downstream
//! (consumed by `roles::classify_nodes`).
//!
//! Both functions read only from the `WindowSnapshot`. The tip set is
//! analytics-internal; only the role outputs cross the wire.
use rustc_hash::{FxHashMap, FxHashSet};

use crate::analytics::snapshot::WindowSnapshot;
use crate::graph::interner::NodeIdx;

const TIP_DEGREE_MIN: u32 = 50;
const TIP_AVG_PER_EDGE_MAX: f64 = 0.01;
const TIP_TOP_N: usize = 8;

/// Identify the top-N tip-account candidates: high-degree wallets with
/// vanishing volume per edge. Sort by degree descending, take the top
/// `TIP_TOP_N`. Same shape as the frontend's `tipCandidates` filter.
pub fn detect_tip_accounts(snapshot: &WindowSnapshot) -> FxHashSet<NodeIdx> {
    let mut candidates: Vec<(NodeIdx, u32)> = snapshot
        .node_stats
        .iter()
        .filter_map(|(&node, stats)| {
            if stats.degree < TIP_DEGREE_MIN {
                return None;
            }
            let avg_per_edge = if stats.degree > 0 {
                stats.volume / stats.degree as f64
            } else {
                0.0
            };
            if avg_per_edge >= TIP_AVG_PER_EDGE_MAX {
                return None;
            }
            Some((node, stats.degree))
        })
        .collect();

    // Sort by degree desc, NodeIdx asc as a stable tiebreaker so a
    // tied snapshot doesn't pick a different top-8 each tick.
    candidates.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    candidates.truncate(TIP_TOP_N);
    candidates.into_iter().map(|(n, _)| n).collect()
}

/// For each non-tip neighbor of a tip account, count how many distinct
/// tip accounts they touch. Used by role classification's mev-searcher
/// rule (`tipsTouched >= MEV_TIPS_TOUCHED_MIN`). Mirrors the
/// `searcherProfile` build at use-raw-stream.ts:591-604.
pub fn build_tips_touched(
    snapshot: &WindowSnapshot,
    tips: &FxHashSet<NodeIdx>,
) -> FxHashMap<NodeIdx, u32> {
    let mut counts: FxHashMap<NodeIdx, u32> = FxHashMap::default();
    for &tip in tips {
        let Some(neighbors) = snapshot.adj.get(&tip) else {
            continue;
        };
        for &other in neighbors.keys() {
            // The frontend includes the neighbor regardless of whether
            // it's also a tip; we mirror that. Role classification's
            // tip-account override fires before mev-searcher anyway, so
            // a tip that touches other tips never gets counted toward
            // its own mev classification.
            *counts.entry(other).or_insert(0) += 1;
        }
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::analytics::snapshot::NodeStats;

    fn empty_snapshot() -> WindowSnapshot {
        WindowSnapshot {
            adj: FxHashMap::default(),
            components: FxHashMap::default(),
            node_stats: FxHashMap::default(),
            mint_addrs: FxHashSet::default(),
        }
    }

    fn stats(degree: u32, volume: f64) -> NodeStats {
        NodeStats {
            degree,
            volume,
            in_vol: 0.0,
            out_vol: volume,
            bidir_vol: 0.0,
            sol_degree: degree,
            spl_degree: 0,
        }
    }

    #[test]
    fn detect_tips_filters_low_degree_and_fat_edges() {
        let mut snap = empty_snapshot();
        // tip-shaped: degree 100, volume 0.5 -> avg 0.005
        snap.node_stats.insert(1, stats(100, 0.5));
        // not tip: degree 100 but avg 1.0
        snap.node_stats.insert(2, stats(100, 100.0));
        // not tip: degree 49 (below threshold)
        snap.node_stats.insert(3, stats(49, 0.001));

        let tips = detect_tip_accounts(&snap);
        assert!(tips.contains(&1));
        assert!(!tips.contains(&2));
        assert!(!tips.contains(&3));
    }

    #[test]
    fn detect_tips_caps_at_top_n() {
        let mut snap = empty_snapshot();
        for i in 0..20u32 {
            snap.node_stats.insert(i, stats(50 + i, 0.001));
        }
        let tips = detect_tip_accounts(&snap);
        assert_eq!(tips.len(), TIP_TOP_N);
    }

    #[test]
    fn tips_touched_counts_distinct_tips_per_neighbor() {
        let mut snap = empty_snapshot();
        // tip 100, tip 101, both touched by node 5
        snap.adj.entry(100).or_default().insert(5, 1.0);
        snap.adj.entry(101).or_default().insert(5, 1.0);
        snap.adj.entry(5).or_default().insert(100, 1.0);
        snap.adj.entry(5).or_default().insert(101, 1.0);

        let mut tips: FxHashSet<NodeIdx> = FxHashSet::default();
        tips.insert(100);
        tips.insert(101);

        let touched = build_tips_touched(&snap, &tips);
        assert_eq!(touched.get(&5).copied(), Some(2));
    }
}
