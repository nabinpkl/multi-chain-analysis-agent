//! `wallet_profile` primitive (Live arm only). Reads the leased
//! `TurnSnapshot` (a per-turn materialized 60s slice opened by
//! `POST /turn/begin`), returns role, community, NodeStats, top
//! counterparties for the requested wallet.
//!
//! Range arm returns `PrimitiveError::NotImplemented` so the Python
//! orchestrator can surface it to the model. Stub registry tracking
//! is gone (Python's responsibility now).

use std::sync::Arc;

use serde::{Deserialize, Serialize};

use super::{PrimitiveError, PrimitiveOutput};
use crate::analytics::NodeRole;
use crate::primitives::types::{NodeStatsWire, ProvenanceRef, TimeScope};
use crate::snapshot::TurnSnapshot;
use crate::state::AppState;

const TOP_K_COUNTERPARTIES: usize = 8;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WalletProfileInput {
    /// Wallet pubkey (base58 Solana address).
    pub addr: String,
    /// Required temporal frame. v0 supports Live; Range routes to a
    /// stubbed warehouse path.
    pub time_scope: TimeScope,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WalletProfileOutput {
    pub addr: String,
    pub role: Option<NodeRole>,
    pub community_id: Option<u32>,
    pub stats: NodeStatsWire,
    pub top_counterparties: Vec<TopCounterparty>,
    pub age_in_window_secs: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TopCounterparty {
    pub addr: String,
    pub volume: f64,
}

/// Compute a `wallet_profile` against the given snapshot. All graph +
/// analytics reads happen against the owned snapshot, never touching
/// the live `GraphState` lock or the analytics watch.
pub async fn compute_with_snapshot(
    _state: &AppState,
    snapshot: &Arc<TurnSnapshot>,
    input: WalletProfileInput,
) -> Result<PrimitiveOutput<WalletProfileOutput>, PrimitiveError> {
    if let TimeScope::Range { .. } = &input.time_scope {
        return Err(PrimitiveError::NotImplemented {
            reason: "wallet_profile Range arm (warehouse path)".into(),
            ship: 5,
        });
    }

    // Window-local interner lookup. `NotInWindow` semantics: the wallet
    // isn't represented in this turn's materialized 60s slice.
    let idx = match snapshot.addr_to_idx.get(&input.addr).copied() {
        Some(i) => i,
        None => {
            return Err(PrimitiveError::NotInWindow {
                addr: input.addr.clone(),
            });
        }
    };

    let stats = snapshot
        .graph
        .node_stats
        .get(&idx)
        .copied()
        .unwrap_or_default();

    // Top-K counterparties by edge weight.
    let neighbors: Vec<(u32, f64, Option<String>)> = snapshot
        .graph
        .adj
        .get(&idx)
        .map(|m| {
            let mut v: Vec<(u32, f64)> = m.iter().map(|(&n, &w)| (n, w)).collect();
            v.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            v.truncate(TOP_K_COUNTERPARTIES);
            v.into_iter()
                .map(|(n, w)| (n, w, snapshot.idx_to_addr.get(&n).cloned()))
                .collect()
        })
        .unwrap_or_default();

    let role: Option<NodeRole> = snapshot.analytics.roles.get(&idx).copied();
    let community_id = snapshot.analytics.labels.get(&idx).copied();

    // Build provenance: focused wallet, neighbor wallets, community,
    // then a Number entry per audit-class stat the model can cite. Names
    // match `NodeStatsWire` field names so they classify identically
    // (Sol for `*_volume_lamports` family via "volume"+"lamport"
    // substring; Count for `*degree` family) in Python's
    // `policy.binding_store._classify_field_name`.
    let mut provenance: Vec<ProvenanceRef> = Vec::new();
    provenance.push(ProvenanceRef::Wallet {
        addr: input.addr.clone(),
        idx: Some(idx),
    });
    for (n_idx, _w, pubkey) in &neighbors {
        if let Some(addr) = pubkey {
            provenance.push(ProvenanceRef::Wallet {
                addr: addr.clone(),
                idx: Some(*n_idx),
            });
        }
    }
    if let Some(c) = community_id {
        provenance.push(ProvenanceRef::Community { id: c });
    }
    let mut support_addrs: Vec<String> = neighbors
        .iter()
        .filter_map(|(_, _, a)| a.clone())
        .collect();
    support_addrs.push(input.addr.clone());
    provenance.push(ProvenanceRef::Number {
        metric: "total_volume_lamports".into(),
        value: stats.volume,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "in_volume_lamports".into(),
        value: stats.in_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "out_volume_lamports".into(),
        value: stats.out_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "bidir_volume_lamports".into(),
        value: stats.bidir_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "degree".into(),
        value: stats.degree as f64,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "sol_degree".into(),
        value: stats.sol_degree as f64,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "spl_degree".into(),
        value: stats.spl_degree as f64,
        support: support_addrs,
    });

    let top_counterparties: Vec<TopCounterparty> = neighbors
        .iter()
        .filter_map(|(_, w, addr)| {
            addr.as_ref().map(|a| TopCounterparty {
                addr: a.clone(),
                volume: *w,
            })
        })
        .collect();

    let value = WalletProfileOutput {
        addr: input.addr,
        role,
        community_id,
        stats: NodeStatsWire::from(&stats),
        top_counterparties,
        age_in_window_secs: 0, // v0 placeholder; needs first-seen tracking
    };

    Ok(PrimitiveOutput {
        value,
        provenance,
        subgraph_slice: None,
    })
}
