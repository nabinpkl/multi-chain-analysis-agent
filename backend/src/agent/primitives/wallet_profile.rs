//! `wallet_profile` primitive (Live arm only in v0). Reads the live
//! `GraphState` under a brief read lock per the analytics-task pattern,
//! returns role, community, NodeStats, and top counterparties for the
//! requested wallet.
//!
//! Range arm registers `primitive.wallet_profile.range_arm` stub and
//! returns `PrimitiveError::NotImplemented`. Ship 5 lands the
//! warehouse path.

use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use std::sync::Arc;

use super::{Primitive, PrimitiveCtx, PrimitiveError, PrimitiveOutput};
use crate::agent::snapshot::{TurnSnapshot, current_time_ms};
use crate::agent::types::{CostClass, DataSource, NodeStatsWire, ProvenanceRef, TimeScope};
use crate::analytics::NodeRole;
use crate::graph::window::window_index;
use crate::state::AppState;

const TOP_K_COUNTERPARTIES: usize = 8;

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WalletProfileInput {
    /// Wallet pubkey (base58 Solana address).
    pub addr: String,
    /// Required temporal frame. v0 supports Live; Range routes to a
    /// stubbed warehouse path.
    pub time_scope: TimeScope,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WalletProfileOutput {
    pub addr: String,
    pub role: Option<NodeRole>,
    pub community_id: Option<u32>,
    pub stats: NodeStatsWire,
    pub top_counterparties: Vec<TopCounterparty>,
    pub age_in_window_secs: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct TopCounterparty {
    pub addr: String,
    pub volume: f64,
}

pub struct WalletProfilePrimitive;

#[async_trait]
impl Primitive for WalletProfilePrimitive {
    type Input = WalletProfileInput;
    type Output = WalletProfileOutput;

    fn name(&self) -> &'static str {
        "wallet_profile"
    }

    fn description(&self) -> &'static str {
        "\
Profile a single Solana wallet observed in the live 60-second window. \
Returns role, community membership, total volume, top counterparties, \
and basic stats. \
\
Use time_scope=Live (the only supported v0 mode) when the user asks \
about \"this wallet\", \"right now\", or \"in the last minute\", or \
refers to entities visible in their current view. \
\
Use Range when they specify an absolute time (NOT IMPLEMENTED IN THIS \
SHIP; returns an error you should surface in your answer). \
\
Always read the <context> block for the focused wallet before guessing.\
"
    }

    fn data_source(&self) -> DataSource {
        DataSource::Live
    }

    fn cost_class(&self) -> CostClass {
        CostClass::Cheap
    }

    /// Ship 4: declare which output fields the diff walker should
    /// compare on a repeat-question replay. SOL volumes use the
    /// default tolerance (10%) so a few-percent drift in a 60s window
    /// is noise; degrees and counterparty membership are compared
    /// strictly. `addr`, `role`, `community_id`, `age_in_window_secs`
    /// are intentionally absent: addr is the focus (always equal by
    /// construction); role/community_id are categorical and rarely
    /// shift mid-window without a primitive value-change worth
    /// reporting separately; age is monotone, always "changed."
    fn diff_spec(&self) -> Vec<(&'static str, crate::agent::diff::FieldKind)> {
        use crate::agent::diff::FieldKind;
        vec![
            ("stats.degree", FieldKind::Count),
            ("stats.total_volume_lamports", FieldKind::number_default()),
            ("stats.in_volume_lamports", FieldKind::number_default()),
            ("stats.out_volume_lamports", FieldKind::number_default()),
            ("stats.bidir_volume_lamports", FieldKind::number_default()),
            ("stats.sol_degree", FieldKind::Count),
            ("stats.spl_degree", FieldKind::Count),
            (
                "top_counterparties",
                FieldKind::EntitySet {
                    key: "addr".to_string(),
                },
            ),
        ]
    }

    async fn execute(
        &self,
        ctx: &PrimitiveCtx<'_>,
        input: Self::Input,
    ) -> Result<PrimitiveOutput<Self::Output>, PrimitiveError> {
        compute(ctx.state, input).await
    }
}

/// Phase A entry point for the Python-agent migration: compute a
/// `wallet_profile` against a pre-built `TurnSnapshot`. The snapshot
/// is owned by the caller (typically the Python orchestrator via the
/// `/primitive/wallet_profile` route, which looks up the snapshot
/// from the lease cache by `snapshot_id`). All graph + analytics
/// reads happen against the owned snapshot, never touching the live
/// `GraphState` lock or the analytics watch.
///
/// The Rust agent loop still calls `compute(state, input)` (below),
/// which builds a one-shot snapshot per call to preserve existing
/// locking semantics until Phase C deletes the loop.
pub async fn compute_with_snapshot(
    state: &AppState,
    snapshot: &Arc<TurnSnapshot>,
    input: WalletProfileInput,
) -> Result<PrimitiveOutput<WalletProfileOutput>, PrimitiveError> {
    // Range arm stub. Hits the stub registry counter and returns
    // a structured NotImplemented; the loop surfaces this to the
    // model as a tool result so it can react.
    if let TimeScope::Range { .. } = &input.time_scope {
        state
            .agent_stubs
            .hit("primitive.wallet_profile.range_arm");
        return Err(PrimitiveError::NotImplemented {
            reason: "wallet_profile Range arm (warehouse path)".into(),
            ship: 5,
        });
    }

    // Look up the focused wallet's idx in the snapshot's window-local
    // interner. Same `NotInWindow` semantics as the pre-snapshot path
    // (the wallet is "not in window" if it's not represented in this
    // turn's materialized 60s slice).
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

    let snap_data = SnapData {
        idx,
        stats,
        neighbors,
    };

    // Build provenance.
    let mut provenance: Vec<ProvenanceRef> = Vec::new();
    provenance.push(ProvenanceRef::Wallet {
        addr: input.addr.clone(),
        idx: Some(snap_data.idx),
    });
    for (n_idx, _w, pubkey) in &snap_data.neighbors {
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
    let mut support_addrs: Vec<String> = snap_data
        .neighbors
        .iter()
        .filter_map(|(_, _, a)| a.clone())
        .collect();
    support_addrs.push(input.addr.clone());
    // Build provenance chip rows for every audit-class stat the
    // model can cite. Names match `NodeStatsWire` field names so
    // they classify identically (Sol for the `*_volume_lamports`
    // family via "volume"+"lamport" substring; Count for the
    // `*degree` family) in `binding_store::build_binding`. With
    // these as provenance entries, the structural value-compare
    // gate finds a matching `ExtractedNumber` in the binding store
    // for any chip the model emits  no Raw-class skip loophole.
    provenance.push(ProvenanceRef::Number {
        metric: "total_volume_lamports".into(),
        value: snap_data.stats.volume,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "in_volume_lamports".into(),
        value: snap_data.stats.in_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "out_volume_lamports".into(),
        value: snap_data.stats.out_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "bidir_volume_lamports".into(),
        value: snap_data.stats.bidir_vol,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "degree".into(),
        value: snap_data.stats.degree as f64,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "sol_degree".into(),
        value: snap_data.stats.sol_degree as f64,
        support: support_addrs.clone(),
    });
    provenance.push(ProvenanceRef::Number {
        metric: "spl_degree".into(),
        value: snap_data.stats.spl_degree as f64,
        support: support_addrs,
    });

    let top_counterparties: Vec<TopCounterparty> = snap_data
        .neighbors
        .iter()
        .filter_map(|(_, w, addr)| addr.as_ref().map(|a| TopCounterparty {
            addr: a.clone(),
            volume: *w,
        }))
        .collect();

    let value = WalletProfileOutput {
        addr: input.addr,
        role,
        community_id,
        stats: NodeStatsWire::from(&snap_data.stats),
        top_counterparties,
        age_in_window_secs: 0, // v0 placeholder; needs first-seen tracking
    };

    Ok(PrimitiveOutput {
        value,
        provenance,
        subgraph_slice: None,
    })
}

/// Back-compat path used by the still-alive Rust agent loop. Builds a
/// one-shot `TurnSnapshot` per call (matches pre-Phase-A locking
/// semantics where each primitive took its own brief read lock and
/// derived its own window snapshot) then delegates to
/// `compute_with_snapshot`. Removed in Phase C alongside the loop.
pub async fn compute(
    state: &AppState,
    input: WalletProfileInput,
) -> Result<PrimitiveOutput<WalletProfileOutput>, PrimitiveError> {
    let live_window_idx = window_index(60).unwrap_or(1);
    let analytics = state
        .analytics
        .snapshots[live_window_idx]
        .borrow()
        .clone();
    let snap = TurnSnapshot::build(
        "oneshot".to_string(),
        live_window_idx,
        60,
        current_time_ms(),
        &state.graph,
        analytics,
    );
    compute_with_snapshot(state, &snap, input).await
}

struct SnapData {
    idx: u32,
    stats: crate::analytics::snapshot::NodeStats,
    neighbors: Vec<(u32, f64, Option<String>)>,
}
