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

use super::{Primitive, PrimitiveCtx, PrimitiveError, PrimitiveOutput};
use crate::agent::types::{
    CostClass, DataSource, NodeStatsWire, NumberRef, ProvenanceRef, TimeScope,
};
use crate::analytics::NodeRole;
use crate::analytics::snapshot::snapshot_window;
use crate::graph::window::window_index;

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

    async fn execute(
        &self,
        ctx: &PrimitiveCtx<'_>,
        input: Self::Input,
    ) -> Result<PrimitiveOutput<Self::Output>, PrimitiveError> {
        // Range arm stub. Hits the stub registry counter and returns
        // a structured NotImplemented; the loop surfaces this to the
        // model as a tool result so it can react.
        if let TimeScope::Range { .. } = &input.time_scope {
            ctx.state
                .agent_stubs
                .hit("primitive.wallet_profile.range_arm");
            return Err(PrimitiveError::NotImplemented {
                reason: "wallet_profile Range arm (warehouse path)".into(),
                ship: 5,
            });
        }

        // Live arm: brief read lock, snapshot, fold to wire shape,
        // release lock, build provenance off-lock. Always operate on
        // the 60s window (idx 1) so per-node stats are computed over
        // a useful slice; the 10s window is too thin and the longer
        // windows are heavier to snapshot.
        let live_window_idx = window_index(60).unwrap_or(1);

        let snap_data = {
            let g = ctx.state.graph.read();
            let idx = match g.lookup_idx(&input.addr) {
                Some(i) => i,
                None => {
                    return Err(PrimitiveError::NotInWindow {
                        addr: input.addr.clone(),
                    });
                }
            };
            let snapshot = snapshot_window(&g, live_window_idx);
            let stats = snapshot
                .node_stats
                .get(&idx)
                .copied()
                .unwrap_or_default();
            let neighbors: Vec<(u32, f64, Option<String>)> = snapshot
                .adj
                .get(&idx)
                .map(|m| {
                    let mut v: Vec<(u32, f64)> =
                        m.iter().map(|(&n, &w)| (n, w)).collect();
                    v.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                    v.truncate(TOP_K_COUNTERPARTIES);
                    v.into_iter()
                        .map(|(n, w)| (n, w, g.lookup_pubkey(n).map(|s| s.to_string())))
                        .collect()
                })
                .unwrap_or_default();
            SnapData {
                idx,
                stats,
                neighbors,
            }
        };

        // Read role + community from analytics watch snapshot. Safe
        // off-lock; this is a separate channel.
        let analytics_snap = ctx
            .state
            .analytics
            .snapshots[live_window_idx]
            .borrow()
            .clone();
        let role = analytics_snap.roles.get(&snap_data.idx).copied();
        let community_id = analytics_snap.labels.get(&snap_data.idx).copied();

        // Build provenance off-lock.
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
        provenance.push(ProvenanceRef::Number {
            metric: "volume".into(),
            value: snap_data.stats.volume,
            support: support_addrs.clone(),
        });
        provenance.push(ProvenanceRef::Number {
            metric: "degree".into(),
            value: snap_data.stats.degree as f64,
            support: support_addrs,
        });

        let _support_numbers: Vec<NumberRef> = vec![
            NumberRef {
                metric: "volume".into(),
                value: snap_data.stats.volume,
            },
            NumberRef {
                metric: "degree".into(),
                value: snap_data.stats.degree as f64,
            },
            NumberRef {
                metric: "in_vol".into(),
                value: snap_data.stats.in_vol,
            },
            NumberRef {
                metric: "out_vol".into(),
                value: snap_data.stats.out_vol,
            },
        ];

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
}

struct SnapData {
    idx: u32,
    stats: crate::analytics::snapshot::NodeStats,
    neighbors: Vec<(u32, f64, Option<String>)>,
}
