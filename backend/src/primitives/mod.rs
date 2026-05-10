//! Pure compute primitives the data plane exposes via `/primitive/*`
//! routes. Phase C reduced this module from the trait+registry layer
//! the dying Rust agent loop needed down to the two compute functions
//! the Python orchestrator actually calls.
//!
//! What's gone since the agent module deletion:
//!  - `Primitive` trait, `PrimitiveCtx`, `PrimitiveRegistry`,
//!    `ErasedPrimitive`, `DispatchOutput`, `ClaimSink`, `SseFrame`
//!  - `PrimitiveBindingStore` + `build_binding` (Python owns these)
//!  - `EmitClaimPrimitive` (Python's loop driver emits Claims now)
//!  - All `diff_spec()` impls (Python's `agent_service/diff.py` carries them)
//!  - The old `compute(state, input)` one-shot path the Rust loop used
//!
//! What remains: `compute_with_snapshot` for `wallet_profile` and
//! `community_summary`, plus the slim internal types in `types.rs`
//! the proto bridge converts to/from.

pub mod community_summary;
pub mod get_token_info;
pub mod types;
pub mod wallet_profile;

use thiserror::Error;

pub use types::{
    CostClass, DataSource, NodeStatsWire, NumberRef, ProvenanceRef, SubgraphSlice, TimeScope,
};

/// Primitive output bundle. Provenance + subgraph_slice flow through
/// the envelope the Python orchestrator decodes; `value` is the
/// primitive-specific output (serialized via serde_json into the
/// envelope's `google.protobuf.Struct` field by the bridge).
pub struct PrimitiveOutput<T> {
    pub value: T,
    pub provenance: Vec<ProvenanceRef>,
    pub subgraph_slice: Option<SubgraphSlice>,
}

#[derive(Debug, Error)]
pub enum PrimitiveError {
    #[error("invalid input: {reason}")]
    InvalidInput { reason: String },
    /// Recoverable from the orchestrator's perspective: the primitive
    /// returned a structured "wallet not in current 60s window" so the
    /// Python loop driver can surface it to the model as a tool result.
    #[error("wallet not in current live window: {addr}")]
    NotInWindow { addr: String },
    /// Range-arm path. Currently surfaced for both wallet_profile and
    /// community_summary when called with `TimeScope::Range`. Lands
    /// for real in ship 5b's warehouse primitives.
    #[error("not implemented: {reason} (lands in ship {ship})")]
    NotImplemented { reason: String, ship: u8 },
    #[error("primitive internal error: {0}")]
    Internal(#[from] anyhow::Error),
}
