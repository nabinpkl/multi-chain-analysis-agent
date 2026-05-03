//! `community_summary` primitive (ship 3). Live arm: walks the
//! analytics snapshot's label map to collect every wallet in a
//! community, folds per-node stats, separates internal vs external
//! transfer volume, and returns a structured summary the model can
//! cite. Range arm registers `primitive.community_summary.range_arm`
//! and returns `NotImplemented { ship: 5 }`. Mirrors the
//! `wallet_profile` shape for consistency.

use std::sync::Arc;

use async_trait::async_trait;
use rustc_hash::FxHashSet;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use super::{Primitive, PrimitiveCtx, PrimitiveError, PrimitiveOutput};
use crate::agent::snapshot::{TurnSnapshot, current_time_ms};
use crate::agent::types::{CostClass, DataSource, ProvenanceRef, TimeScope};
use crate::graph::window::window_index;
use crate::state::AppState;

/// Cap on how many representative wallets we surface. 8 keeps the
/// output bounded and matches `wallet_profile`'s top_counterparties
/// cap; the model uses these as anchors for narrative ("the largest
/// member is ..."), it doesn't need every member.
const TOP_K_WALLETS: usize = 8;

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct CommunitySummaryInput {
    /// Stable community label (`u32`). Source it from a prior
    /// `wallet_profile` response (the `community_id` field) or from
    /// the user's selection on the live graph.
    pub community_id: u32,
    /// Required temporal frame. v0 supports Live; Range routes to a
    /// stubbed warehouse path.
    pub time_scope: TimeScope,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct CommunitySummaryOutput {
    pub community_id: u32,
    /// Number of distinct wallets carrying this label in the live
    /// 60s window.
    pub size: u32,
    /// Total transfer volume touching any community member,
    /// double-counting (a member-to-member edge contributes once via
    /// each endpoint). Matches the `volume` semantics on
    /// `wallet_profile.stats`.
    pub total_volume: f64,
    /// Sum of transfer-edge weights between two community members.
    /// Single-counted (each edge contributes once).
    pub internal_volume: f64,
    /// Sum of transfer-edge weights crossing the community boundary
    /// (one endpoint inside, one outside). Single-counted.
    pub external_volume: f64,
    /// Number of edges between community members (single-counted).
    pub edge_count: u32,
    /// Top members by degree, capped at `TOP_K_WALLETS`. Ties
    /// broken by larger volume.
    pub top_wallets: Vec<TopWallet>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct TopWallet {
    pub addr: String,
    pub degree: u32,
    pub volume: f64,
}

pub struct CommunitySummaryPrimitive;

#[async_trait]
impl Primitive for CommunitySummaryPrimitive {
    type Input = CommunitySummaryInput;
    type Output = CommunitySummaryOutput;

    fn name(&self) -> &'static str {
        "community_summary"
    }

    fn description(&self) -> &'static str {
        "\
Summarize a single community in the live 60-second window. Returns \
size (member count), total volume, internal/external volume split, \
edge count, and top members by degree. \
\
Use after `wallet_profile` has revealed a `community_id` you want to \
characterize, or when the user asks about a specific community on \
the live graph. \
\
time_scope=Live is the only supported v0 mode; Range returns a not- \
implemented error you should surface in your answer.\
"
    }

    fn data_source(&self) -> DataSource {
        DataSource::Live
    }

    fn cost_class(&self) -> CostClass {
        CostClass::Cheap
    }

    /// Ship 4: declare which output fields the diff walker should
    /// compare. Volumes use default tolerance (10%); size + edge
    /// count are exact-compare counts. `top_wallets` is set-membership
    /// by `addr`; ordering shifts (rotation in/out of top-K) surface
    /// as added/removed pairs only when membership actually changes.
    /// `community_id` is omitted because it's the focus identifier;
    /// always equal by construction.
    fn diff_spec(&self) -> Vec<(&'static str, crate::agent::diff::FieldKind)> {
        use crate::agent::diff::FieldKind;
        vec![
            ("size", FieldKind::Count),
            ("total_volume", FieldKind::number_default()),
            ("internal_volume", FieldKind::number_default()),
            ("external_volume", FieldKind::number_default()),
            ("edge_count", FieldKind::Count),
            (
                "top_wallets",
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
/// `community_summary` against a pre-built `TurnSnapshot`. Same
/// rationale as `wallet_profile::compute_with_snapshot`: all reads
/// against the owned snapshot, no live-graph lock or analytics
/// watch touched.
pub async fn compute_with_snapshot(
    state: &AppState,
    snapshot: &Arc<TurnSnapshot>,
    input: CommunitySummaryInput,
) -> Result<PrimitiveOutput<CommunitySummaryOutput>, PrimitiveError> {
    if let TimeScope::Range { .. } = &input.time_scope {
        state
            .agent_stubs
            .hit("primitive.community_summary.range_arm");
        return Err(PrimitiveError::NotImplemented {
            reason: "community_summary Range arm (warehouse path)".into(),
            ship: 5,
        });
    }

    // 1. Member set from cloned analytics labels in the snapshot.
    let member_idxs: FxHashSet<u32> = snapshot
        .analytics
        .labels
        .iter()
        .filter_map(|(&idx, &cid)| if cid == input.community_id { Some(idx) } else { None })
        .collect();
    if member_idxs.is_empty() {
        return Err(PrimitiveError::NotInWindow {
            addr: format!("community#{}", input.community_id),
        });
    }

    // 2. Walk adjacency from the snapshot to split internal / external
    //    volume. Same single-counting trick (a < b) as the pre-Phase-A
    //    path. No lock to acquire; everything is owned data already.
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

    // 4. Pick the top-K members by degree (volume tie-break).
    member_summaries.sort_by(|a, b| {
        b.degree
            .cmp(&a.degree)
            .then_with(|| b.volume.partial_cmp(&a.volume).unwrap_or(std::cmp::Ordering::Equal))
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

/// Back-compat path used by the still-alive Rust agent loop. Builds
/// a one-shot `TurnSnapshot` per call. Removed in Phase C.
pub async fn compute(
    state: &AppState,
    input: CommunitySummaryInput,
) -> Result<PrimitiveOutput<CommunitySummaryOutput>, PrimitiveError> {
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
