//! Per-node role classification. Direct port of
//! `frontend/src/lib/role-detect.ts:classifyNodes`. Same thresholds,
//! same resolution order ("first match wins"):
//!
//!   token-mint -> tip-account -> mev-searcher -> multi-hub
//!   -> sol-hub -> spl-hub -> whale -> mpc-member -> normal
//!
//! Operates on the `WindowSnapshot` plus precomputed tip + MPC member
//! sets and the `tipsTouched` map (built by `mev.rs`). The result is
//! the only output of the analytics task that crosses the wire as
//! per-node labels (community ids are the other).
use rustc_hash::{FxHashMap, FxHashSet};
use serde::Serialize;
use ts_rs::TS;

use crate::analytics::snapshot::WindowSnapshot;
use crate::graph::interner::NodeIdx;

// MEV searcher signature: many tip-account touches AND near-zero non-tip
// SOL footprint. Heavy bots paying every shift have 7-8 tip-account
// touches and effectively zero non-tip SOL flow because their profits
// are SPL-token denominated and invisible to our v0 parser.
const MEV_TIPS_TOUCHED_MIN: u32 = 7;
const MEV_MAX_SOL_FOOTPRINT: f64 = 0.01;

// Hub signature: connectivity only, no amount filter. 50+ unique
// counterparties is structurally a hub regardless of value moved.
// Sub-classification (sol/spl/multi) uses binary presence of SOL vs
// SPL neighbors.
const HUB_DEGREE_MIN: u32 = 50;

// Whale signature: a few big counterparties. OTC pattern.
const WHALE_VOLUME_MIN: f64 = 100.0;
const WHALE_DEGREE_MAX: u32 = 10;

/// Wire-stable per-node role tag. Kebab-case strings on the wire so the
/// frontend's existing union type
/// (`"token-mint" | "tip-account" | ...`) reads them unchanged.
#[derive(
    Serialize,
    serde::Deserialize,
    TS,
    Clone,
    Copy,
    Debug,
    PartialEq,
    Eq,
    Hash,
)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum NodeRole {
    TokenMint,
    TipAccount,
    MevSearcher,
    MultiHub,
    SolHub,
    SplHub,
    Whale,
    MpcMember,
    Normal,
}

/// Classify every node in the snapshot. Resolution order matches the
/// frontend exactly. Nodes not in the returned map are implicitly
/// `Normal`; we still emit explicit entries for downstream diffing
/// against `prev_roles` so the wire format can express "node N is
/// gone" via removals.
pub fn classify_nodes(
    snapshot: &WindowSnapshot,
    tip_addrs: &FxHashSet<NodeIdx>,
    mpc_members: &FxHashSet<NodeIdx>,
    tips_touched: &FxHashMap<NodeIdx, u32>,
) -> FxHashMap<NodeIdx, NodeRole> {
    let mut roles: FxHashMap<NodeIdx, NodeRole> = FxHashMap::default();

    // Iterate every node that appears in this window's snapshot.
    // `node_stats` covers all of them (including isolated nodes? no,
    // isolated nodes don't appear in window edges, so they're not in
    // this window's snapshot to begin with). The frontend iterates
    // graphology's nodes; the snapshot's node set is the equivalent.
    for (&node, stats) in &snapshot.node_stats {
        // 1. token-mint override fires first regardless of other
        //    signals. Mint pubkeys are token contracts; a popular
        //    meme-coin can rack up thousands of tiny-edge recipients
        //    that look exactly like a tip account.
        if snapshot.mint_addrs.contains(&node) {
            roles.insert(node, NodeRole::TokenMint);
            continue;
        }

        // 2. tip-account: precomputed via mev::detect_tip_accounts.
        if tip_addrs.contains(&node) {
            roles.insert(node, NodeRole::TipAccount);
            continue;
        }

        // 3. mev-searcher: touches >=7 tip accounts AND has near-zero
        //    non-tip SOL footprint.
        let tips_count = tips_touched.get(&node).copied().unwrap_or(0);
        if tips_count >= MEV_TIPS_TOUCHED_MIN
            && (stats.in_vol + stats.out_vol) < MEV_MAX_SOL_FOOTPRINT
        {
            roles.insert(node, NodeRole::MevSearcher);
            continue;
        }

        // 4. Hub labels: connectivity-only. Sub-type by binary presence
        //    of SOL vs SPL neighbors.
        if stats.degree >= HUB_DEGREE_MIN {
            let has_sol = stats.sol_degree >= 1;
            let has_spl = stats.spl_degree >= 1;
            if has_sol && has_spl {
                roles.insert(node, NodeRole::MultiHub);
                continue;
            }
            if has_sol {
                roles.insert(node, NodeRole::SolHub);
                continue;
            }
            if has_spl {
                roles.insert(node, NodeRole::SplHub);
                continue;
            }
        }

        // 5. whale: high SOL volume concentrated in a few edges.
        if stats.volume >= WHALE_VOLUME_MIN && stats.degree <= WHALE_DEGREE_MAX {
            roles.insert(node, NodeRole::Whale);
            continue;
        }

        // 6. mpc-member: catch-all for nodes inside a flagged community.
        if mpc_members.contains(&node) {
            roles.insert(node, NodeRole::MpcMember);
            continue;
        }

        roles.insert(node, NodeRole::Normal);
    }

    roles
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

    fn stats(
        degree: u32,
        volume: f64,
        in_vol: f64,
        out_vol: f64,
        sol_degree: u32,
        spl_degree: u32,
    ) -> NodeStats {
        NodeStats {
            degree,
            volume,
            in_vol,
            out_vol,
            bidir_vol: 0.0,
            sol_degree,
            spl_degree,
        }
    }

    #[test]
    fn token_mint_overrides_high_fanout() {
        let mut snap = empty_snapshot();
        // High degree + dust per edge would otherwise look like a tip.
        snap.node_stats.insert(1, stats(200, 0.5, 0.0, 0.5, 200, 0));
        snap.mint_addrs.insert(1);
        let tips: FxHashSet<NodeIdx> = vec![1].into_iter().collect();
        let mpc_members: FxHashSet<NodeIdx> = FxHashSet::default();
        let tips_touched: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        let roles = classify_nodes(&snap, &tips, &mpc_members, &tips_touched);
        assert_eq!(roles.get(&1).copied(), Some(NodeRole::TokenMint));
    }

    #[test]
    fn whale_classified_after_tip_and_hub_misses() {
        let mut snap = empty_snapshot();
        // High volume, low degree. Doesn't match tip (low degree),
        // doesn't match hub (low degree), passes whale.
        snap.node_stats.insert(2, stats(5, 500.0, 200.0, 300.0, 5, 0));
        let tips: FxHashSet<NodeIdx> = FxHashSet::default();
        let mpc_members: FxHashSet<NodeIdx> = FxHashSet::default();
        let tips_touched: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        let roles = classify_nodes(&snap, &tips, &mpc_members, &tips_touched);
        assert_eq!(roles.get(&2).copied(), Some(NodeRole::Whale));
    }

    #[test]
    fn multi_hub_set_when_both_kinds_present() {
        let mut snap = empty_snapshot();
        snap.node_stats.insert(3, stats(60, 5.0, 2.0, 3.0, 30, 30));
        let roles = classify_nodes(
            &snap,
            &FxHashSet::default(),
            &FxHashSet::default(),
            &FxHashMap::default(),
        );
        assert_eq!(roles.get(&3).copied(), Some(NodeRole::MultiHub));
    }

    #[test]
    fn mev_searcher_requires_low_sol_footprint() {
        let mut snap = empty_snapshot();
        // Touches 7 tips, but its SOL footprint exceeds MEV_MAX_SOL_FOOTPRINT.
        // Should NOT classify as mev-searcher; falls through to hub or normal.
        snap.node_stats.insert(4, stats(7, 5.0, 2.0, 3.0, 7, 0));
        let mut tips_touched: FxHashMap<NodeIdx, u32> = FxHashMap::default();
        tips_touched.insert(4, 7);
        let roles = classify_nodes(
            &snap,
            &FxHashSet::default(),
            &FxHashSet::default(),
            &tips_touched,
        );
        assert_ne!(roles.get(&4).copied(), Some(NodeRole::MevSearcher));
    }

    #[test]
    fn normal_default_when_no_signals() {
        let mut snap = empty_snapshot();
        snap.node_stats.insert(5, stats(2, 0.001, 0.001, 0.0, 2, 0));
        let roles = classify_nodes(
            &snap,
            &FxHashSet::default(),
            &FxHashSet::default(),
            &FxHashMap::default(),
        );
        assert_eq!(roles.get(&5).copied(), Some(NodeRole::Normal));
    }
}
