//! `community_summary` primitive (Live arm only). Walks the leased
//! `TurnSnapshot`'s analytics labels for every wallet in a community,
//! folds per-node stats, separates internal vs external transfer
//! volume, returns a structured summary the model can cite.
//!
//! Range arm returns `PrimitiveError::NotImplemented` for the Python
//! orchestrator to surface to the model. Stub registry tracking is
//! gone (Python's responsibility now).

use std::sync::Arc;

use rustc_hash::FxHashSet;
use serde::{Deserialize, Serialize};

use super::{PrimitiveError, PrimitiveOutput};
use crate::primitives::types::{ProvenanceRef, TimeScope};
use crate::snapshot::TurnSnapshot;
use crate::state::AppState;

const TOP_K_WALLETS: usize = 8;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommunitySummaryInput {
    /// Stable community label (`u32`). Source it from a prior
    /// `wallet_profile` response (the `community_id` field) or from
    /// the user's selection on the live graph.
    pub community_id: u32,
    /// Required temporal frame. v0 supports Live; Range routes to a
    /// stubbed warehouse path.
    pub time_scope: TimeScope,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommunitySummaryOutput {
    pub community_id: u32,
    pub size: u32,
    pub total_volume: f64,
    pub internal_volume: f64,
    pub external_volume: f64,
    pub edge_count: u32,
    pub top_wallets: Vec<TopWallet>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TopWallet {
    pub addr: String,
    pub degree: u32,
    pub volume: f64,
}

pub async fn compute_with_snapshot(
    _state: &AppState,
    snapshot: &Arc<TurnSnapshot>,
    input: CommunitySummaryInput,
) -> Result<PrimitiveOutput<CommunitySummaryOutput>, PrimitiveError> {
    if let TimeScope::Range { .. } = &input.time_scope {
        return Err(PrimitiveError::NotImplemented {
            reason: "community_summary Range arm (warehouse path)".into(),
            ship: 5,
        });
    }

    // 1. Member set from the snapshot's analytics labels.
    let member_idxs: FxHashSet<u32> = snapshot
        .analytics
        .labels
        .iter()
        .filter_map(|(&idx, &cid)| {
            if cid == input.community_id {
                Some(idx)
            } else {
                None
            }
        })
        .collect();
    if member_idxs.is_empty() {
        return Err(PrimitiveError::NotInWindow {
            addr: format!("community#{}", input.community_id),
        });
    }

    // 2. Walk adjacency to split internal / external volume. Single-
    // counting via `a < b` to avoid double-counting undirected edges.
    let mut internal_volume = 0.0_f64;
    let mut external_volume = 0.0_f64;
    let mut edge_count: u32 = 0;
    for (&a, neighbors) in snapshot.graph.adj.iter() {
        let a_in = member_idxs.contains(&a);
        for (&b, &w) in neighbors.iter() {
            if a >= b {
                continue;
            }
            let b_in = member_idxs.contains(&b);
            match (a_in, b_in) {
                (true, true) => {
                    internal_volume += w;
                    edge_count = edge_count.saturating_add(1);
                }
                (true, false) | (false, true) => {
                    external_volume += w;
                }
                (false, false) => {}
            }
        }
    }

    // 3. Fold per-member stats.
    let mut total_volume = 0.0_f64;
    let mut member_summaries: Vec<TopWallet> = Vec::with_capacity(member_idxs.len());
    for &idx in &member_idxs {
        let stats = snapshot
            .graph
            .node_stats
            .get(&idx)
            .copied()
            .unwrap_or_default();
        total_volume += stats.volume;
        if let Some(addr) = snapshot.idx_to_addr.get(&idx) {
            member_summaries.push(TopWallet {
                addr: addr.clone(),
                degree: stats.degree,
                volume: stats.volume,
            });
        }
    }

    // 4. Pick top-K members by degree (volume tie-break).
    member_summaries.sort_by(|a, b| {
        b.degree.cmp(&a.degree).then_with(|| {
            b.volume
                .partial_cmp(&a.volume)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
    });
    member_summaries.truncate(TOP_K_WALLETS);

    // 5. Build provenance.
    let mut provenance: Vec<ProvenanceRef> = Vec::new();
    provenance.push(ProvenanceRef::Community {
        id: input.community_id,
    });
    for tw in &member_summaries {
        provenance.push(ProvenanceRef::Wallet {
            addr: tw.addr.clone(),
            idx: None,
        });
    }
    let support_addrs: Vec<String> = member_summaries.iter().map(|t| t.addr.clone()).collect();
    provenance.push(ProvenanceRef::Number {
        metric: "size".into(),
        value: member_idxs.len() as f64,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "total_volume".into(),
        value: total_volume,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "internal_volume".into(),
        value: internal_volume,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "external_volume".into(),
        value: external_volume,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "edge_count".into(),
        value: edge_count as f64,
        support: support_addrs,
    });

    let value = CommunitySummaryOutput {
        community_id: input.community_id,
        size: member_idxs.len() as u32,
        total_volume,
        internal_volume,
        external_volume,
        edge_count,
        top_wallets: member_summaries,
    };

    Ok(PrimitiveOutput {
        value,
        provenance,
        subgraph_slice: None,
    })
}
