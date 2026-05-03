//! TRANSITIONAL: bridge between proto-generated wire types
//! (`crate::wire::generated::multichain::wire::shared::v1::*`) and the
//! pre-protobuf internal Rust types living under `crate::agent::*`.
//!
//! This module exists for exactly one reason: the Rust agent loop
//! (`backend/src/agent/loop.rs`) still consumes the internal types and
//! has not yet been deleted. Phase C deletes the loop and the internal
//! types in the same commit; this whole file goes with it. Until then,
//! the `/primitive/*` HTTP boundary in `api::primitives` decodes proto
//! requests, bridges them to internal types so `compute_with_snapshot`
//! can run unchanged, then bridges the internal output back to proto
//! for the binary-protobuf response.
//!
//! No serde here. The proto generated types do not derive serde; their
//! wire format is binary protobuf. JSON-fallback for curl debugging
//! lives on a parallel path in `api::primitives` that uses the legacy
//! serde types directly until Stage 4 deletes them.

#![allow(dead_code)] // some helpers (e.g. NodeRole reverse map) used only by one direction

use crate::agent::primitives::community_summary as cs_internal;
use crate::agent::primitives::wallet_profile as wp_internal;
use crate::agent::types as internal;
use crate::analytics::roles::NodeRole as InternalNodeRole;
use crate::wire::generated::multichain::wire::shared::v1 as proto;
use crate::wire::generated::multichain::wire::shared::v1::__buffa::oneof as proto_oneof;
use buffa::{EnumValue, MessageField};

// ---------------------------------------------------------------------------
// Errors. The HTTP boundary maps these to 400.
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum BridgeError {
    #[error("missing required proto field: {0}")]
    MissingField(&'static str),
    #[error("unset oneof on {0}")]
    UnsetOneof(&'static str),
    #[error("invalid value for {0}")]
    InvalidValue(&'static str),
}

// ---------------------------------------------------------------------------
// TimeScope (proto oneof Live/Range  internal externally-tagged enum)
// ---------------------------------------------------------------------------

pub fn proto_to_internal_time_scope(
    p: &proto::TimeScope,
) -> Result<internal::TimeScope, BridgeError> {
    match &p.scope {
        Some(proto_oneof::time_scope::Scope::Live(_)) => Ok(internal::TimeScope::Live),
        Some(proto_oneof::time_scope::Scope::Range(r)) => Ok(internal::TimeScope::Range {
            from_s: r.from_s,
            to_s: r.to_s,
        }),
        None => Err(BridgeError::UnsetOneof("TimeScope.scope")),
    }
}

// ---------------------------------------------------------------------------
// NodeRole (proto enum NODE_ROLE_*  internal kebab-case enum)
// ---------------------------------------------------------------------------

fn internal_to_proto_node_role(r: InternalNodeRole) -> proto::NodeRole {
    match r {
        InternalNodeRole::TokenMint => proto::NodeRole::NODE_ROLE_TOKEN_MINT,
        InternalNodeRole::TipAccount => proto::NodeRole::NODE_ROLE_TIP_ACCOUNT,
        InternalNodeRole::MevSearcher => proto::NodeRole::NODE_ROLE_MEV_SEARCHER,
        InternalNodeRole::MultiHub => proto::NodeRole::NODE_ROLE_MULTI_HUB,
        InternalNodeRole::SolHub => proto::NodeRole::NODE_ROLE_SOL_HUB,
        InternalNodeRole::SplHub => proto::NodeRole::NODE_ROLE_SPL_HUB,
        InternalNodeRole::Whale => proto::NodeRole::NODE_ROLE_WHALE,
        InternalNodeRole::MpcMember => proto::NodeRole::NODE_ROLE_MPC_MEMBER,
        InternalNodeRole::Normal => proto::NodeRole::NODE_ROLE_NORMAL,
    }
}

// ---------------------------------------------------------------------------
// NodeStatsWire (internal struct  proto::NodeStats; field-for-field)
// ---------------------------------------------------------------------------

fn internal_to_proto_node_stats(s: &internal::NodeStatsWire) -> proto::NodeStats {
    proto::NodeStats {
        degree: s.degree,
        total_volume_lamports: s.total_volume_lamports,
        in_volume_lamports: s.in_volume_lamports,
        out_volume_lamports: s.out_volume_lamports,
        bidir_volume_lamports: s.bidir_volume_lamports,
        sol_degree: s.sol_degree,
        spl_degree: s.spl_degree,
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// ProvenanceRef (internal externally-tagged enum  proto oneof)
// ---------------------------------------------------------------------------

fn internal_to_proto_provenance(r: internal::ProvenanceRef) -> proto::ProvenanceRef {
    let ref_oneof = match r {
        internal::ProvenanceRef::Wallet { addr, idx } => {
            proto_oneof::provenance_ref::Ref::Wallet(Box::new(proto::WalletRef {
                addr,
                idx,
                ..Default::default()
            }))
        }
        internal::ProvenanceRef::Edge { id, src, dst } => {
            proto_oneof::provenance_ref::Ref::Edge(Box::new(proto::EdgeRef {
                id,
                src,
                dst,
                ..Default::default()
            }))
        }
        internal::ProvenanceRef::Community { id } => {
            proto_oneof::provenance_ref::Ref::Community(Box::new(proto::CommunityRef {
                id,
                ..Default::default()
            }))
        }
        internal::ProvenanceRef::TimeRange { from_s, to_s } => {
            proto_oneof::provenance_ref::Ref::TimeRange(Box::new(proto::TimeRangeRef {
                from_s,
                to_s,
                ..Default::default()
            }))
        }
        internal::ProvenanceRef::Number {
            metric,
            value,
            support,
        } => proto_oneof::provenance_ref::Ref::Number(Box::new(proto::NumberRef {
            metric,
            value,
            support,
            ..Default::default()
        })),
    };
    proto::ProvenanceRef {
        r#ref: Some(ref_oneof),
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// SubgraphSlice (internal  proto). Currently always None on the
// primitive output path; included for completeness so envelope
// construction is total.
// ---------------------------------------------------------------------------

fn internal_to_proto_subgraph_slice(s: internal::SubgraphSlice) -> proto::SubgraphSlice {
    proto::SubgraphSlice {
        nodes: s
            .nodes
            .into_iter()
            .map(|n| proto::NodeSummary {
                addr: n.addr,
                role: n.role,
                ..Default::default()
            })
            .collect(),
        edges: s
            .edges
            .into_iter()
            .map(|e| proto::EdgeSummary {
                src: e.src,
                dst: e.dst,
                volume: e.volume,
                ..Default::default()
            })
            .collect(),
        time_range: match s.time_range {
            Some(tr) => MessageField::some(proto::TimeRange {
                from_s: tr.from_s,
                to_s: tr.to_s,
                ..Default::default()
            }),
            None => MessageField::none(),
        },
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// WalletProfile request / output bridging
// ---------------------------------------------------------------------------

pub fn proto_to_internal_wallet_input(
    p: proto::WalletProfileInput,
) -> Result<wp_internal::WalletProfileInput, BridgeError> {
    if p.addr.is_empty() {
        return Err(BridgeError::MissingField("WalletProfileInput.addr"));
    }
    let ts = p
        .time_scope
        .into_option()
        .ok_or(BridgeError::MissingField("WalletProfileInput.time_scope"))?;
    let time_scope = proto_to_internal_time_scope(&ts)?;
    Ok(wp_internal::WalletProfileInput {
        addr: p.addr,
        time_scope,
    })
}

pub fn internal_to_proto_wallet_output(
    out: wp_internal::WalletProfileOutput,
) -> proto::WalletProfileOutput {
    proto::WalletProfileOutput {
        addr: out.addr,
        role: out
            .role
            .map(|r| EnumValue::Known(internal_to_proto_node_role(r)))
            .unwrap_or(EnumValue::Known(proto::NodeRole::NODE_ROLE_UNSPECIFIED)),
        community_id: out.community_id,
        stats: MessageField::some(internal_to_proto_node_stats(&out.stats)),
        top_counterparties: out
            .top_counterparties
            .into_iter()
            .map(|tc| proto::TopCounterparty {
                addr: tc.addr,
                volume: tc.volume,
                ..Default::default()
            })
            .collect(),
        age_in_window_secs: out.age_in_window_secs,
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// CommunitySummary request / output bridging
// ---------------------------------------------------------------------------

pub fn proto_to_internal_community_input(
    p: proto::CommunitySummaryInput,
) -> Result<cs_internal::CommunitySummaryInput, BridgeError> {
    let ts = p
        .time_scope
        .into_option()
        .ok_or(BridgeError::MissingField("CommunitySummaryInput.time_scope"))?;
    let time_scope = proto_to_internal_time_scope(&ts)?;
    Ok(cs_internal::CommunitySummaryInput {
        community_id: p.community_id,
        time_scope,
    })
}

pub fn internal_to_proto_community_output(
    out: cs_internal::CommunitySummaryOutput,
) -> proto::CommunitySummaryOutput {
    proto::CommunitySummaryOutput {
        community_id: out.community_id,
        size: out.size,
        total_volume: out.total_volume,
        internal_volume: out.internal_volume,
        external_volume: out.external_volume,
        edge_count: out.edge_count,
        top_wallets: out
            .top_wallets
            .into_iter()
            .map(|t| proto::TopWallet {
                addr: t.addr,
                degree: t.degree,
                volume: t.volume,
                ..Default::default()
            })
            .collect(),
        ..Default::default()
    }
}

// ---------------------------------------------------------------------------
// Envelope construction. Both per-primitive `value` types serialize to
// JSON via serde, and the resulting `serde_json::Value` deserializes
// straight into `buffa_types::google::protobuf::Struct` via the serde
// impls in `buffa-types::value_ext`. That gives us a typed proto
// envelope without a per-primitive oneof discriminator (deferred; the
// `value` field is `google.protobuf.Struct` per the current proto).
// ---------------------------------------------------------------------------

fn json_to_proto_struct(
    v: serde_json::Value,
) -> Result<buffa_types::google::protobuf::Struct, BridgeError> {
    // The proto Struct only represents an object at the top level. Wrap
    // anything else (shouldn't happen for primitive outputs which are
    // always objects) so `serde_json::from_value::<Struct>` succeeds.
    let object = match v {
        serde_json::Value::Object(_) => v,
        other => {
            let mut m = serde_json::Map::new();
            m.insert("value".to_string(), other);
            serde_json::Value::Object(m)
        }
    };
    serde_json::from_value(object).map_err(|_| BridgeError::InvalidValue("envelope.value"))
}

pub fn build_envelope<T: serde::Serialize>(
    out: crate::agent::primitives::PrimitiveOutput<T>,
) -> Result<proto::PrimitiveResponseEnvelope, BridgeError> {
    let value_json = serde_json::to_value(&out.value)
        .map_err(|_| BridgeError::InvalidValue("envelope.value (serialize)"))?;
    let value_struct = json_to_proto_struct(value_json)?;
    let provenance = out
        .provenance
        .into_iter()
        .map(internal_to_proto_provenance)
        .collect();
    let subgraph_slice = match out.subgraph_slice {
        Some(s) => MessageField::some(internal_to_proto_subgraph_slice(s)),
        None => MessageField::none(),
    };
    Ok(proto::PrimitiveResponseEnvelope {
        value: MessageField::some(value_struct),
        provenance,
        subgraph_slice,
        ..Default::default()
    })
}
