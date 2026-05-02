//! `community_summary` primitive (ship 3). Live arm: walks the
//! analytics snapshot's label map to collect every wallet in a
//! community, folds per-node stats, separates internal vs external
//! transfer volume, and returns a structured summary the model can
//! cite. Range arm registers `primitive.community_summary.range_arm`
//! and returns `NotImplemented { ship: 5 }`. Mirrors the
//! `wallet_profile` shape for consistency.

use async_trait::async_trait;
use rustc_hash::FxHashSet;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use super::{Primitive, PrimitiveCtx, PrimitiveError, PrimitiveOutput};
use crate::agent::types::{CostClass, DataSource, ProvenanceRef, TimeScope};
use crate::analytics::snapshot::snapshot_window;
use crate::graph::window::window_index;

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
        if let TimeScope::Range { .. } = &input.time_scope {
            ctx.state
                .agent_stubs
                .hit("primitive.community_summary.range_arm");
            return Err(PrimitiveError::NotImplemented {
                reason: "community_summary Range arm (warehouse path)".into(),
                ship: 5,
            });
        }

        // Live arm: 60s window matches wallet_profile so the two
        // primitives' numbers are directly comparable.
        let live_window_idx = window_index(60).unwrap_or(1);

        // 1. Pull the community label set from the analytics snapshot.
        // The labels watch is a separate channel from the graph; safe
        // to read off-lock.
        let analytics_snap = ctx
            .state
            .analytics
            .snapshots[live_window_idx]
            .borrow()
            .clone();
        let member_idxs: FxHashSet<u32> = analytics_snap
            .labels
            .iter()
            .filter_map(|(&idx, &cid)| if cid == input.community_id { Some(idx) } else { None })
            .collect();
        if member_idxs.is_empty() {
            return Err(PrimitiveError::NotInWindow {
                addr: format!("community#{}", input.community_id),
            });
        }

        // 2. Snapshot the graph window. Read lock is brief; all
        // subsequent work happens off-lock against the owned
        // snapshot. Build addr lookups while we have the lock.
        struct WalkData {
            snap_node_stats:
                rustc_hash::FxHashMap<u32, crate::analytics::snapshot::NodeStats>,
            internal_volume: f64,
            external_volume: f64,
            edge_count: u32,
            addr_lookup: rustc_hash::FxHashMap<u32, String>,
        }
        let walk: WalkData = {
            let g = ctx.state.graph.read();
            let snapshot = snapshot_window(&g, live_window_idx);

            // Walk adjacency to compute internal vs external volume.
            // adjacency `adj[a][b] = w` is undirected and double-
            // entered (a->b and b->a both present). We single-count
            // by iterating only when a < b.
            let mut internal_volume = 0.0_f64;
            let mut external_volume = 0.0_f64;
            let mut edge_count: u32 = 0;
            for (&a, neighbors) in snapshot.adj.iter() {
                let a_in = member_idxs.contains(&a);
                for (&b, &w) in neighbors.iter() {
                    if a >= b {
                        // Single-count: only handle the canonical
                        // direction, skip the reverse entry.
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

            let addr_lookup: rustc_hash::FxHashMap<u32, String> = member_idxs
                .iter()
                .filter_map(|&idx| g.lookup_pubkey(idx).map(|s| (idx, s.to_string())))
                .collect();

            WalkData {
                snap_node_stats: snapshot.node_stats,
                internal_volume,
                external_volume,
                edge_count,
                addr_lookup,
            }
        };

        // 3. Fold per-member stats.
        let mut total_volume = 0.0_f64;
        let mut member_summaries: Vec<TopWallet> = Vec::with_capacity(member_idxs.len());
        for &idx in &member_idxs {
            let stats = walk
                .snap_node_stats
                .get(&idx)
                .copied()
                .unwrap_or_default();
            total_volume += stats.volume;
            if let Some(addr) = walk.addr_lookup.get(&idx) {
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

        // 5. Build provenance: community ref + each top wallet ref +
        // structured Number refs the binding store walks. Numbers
        // mirror the output fields so the binding ledger has a clean
        // mapping from output → ExtractedNumber → unit class.
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
        let support_addrs: Vec<String> =
            member_summaries.iter().map(|t| t.addr.clone()).collect();
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
            value: walk.internal_volume,
            support: support_addrs.clone(),
        });
        provenance.push(ProvenanceRef::Number {
            metric: "external_volume".into(),
            value: walk.external_volume,
            support: support_addrs.clone(),
        });
        provenance.push(ProvenanceRef::Number {
            metric: "edge_count".into(),
            value: walk.edge_count as f64,
            support: support_addrs,
        });

        let value = CommunitySummaryOutput {
            community_id: input.community_id,
            size: member_idxs.len() as u32,
            total_volume,
            internal_volume: walk.internal_volume,
            external_volume: walk.external_volume,
            edge_count: walk.edge_count,
            top_wallets: member_summaries,
        };

        Ok(PrimitiveOutput {
            value,
            provenance,
            subgraph_slice: None,
        })
    }
}
