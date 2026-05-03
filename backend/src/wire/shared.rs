//! Shared wire types: anything that crosses a service boundary AND is
//! the same shape on both sides. Lives in one place so codegen has a
//! single inventory to walk.
//!
//! Phase A approach: rather than relocate every existing type (which
//! would touch hundreds of imports during a migration), this module
//! `pub use`s types from their current locations. The
//! `JsonSchema`-deriving binary `dump_schemas.rs` enumerates the same
//! list and writes per-type schemas. New types added during the
//! migration (snapshot lease envelopes, primitive request wrappers)
//! are defined directly here. As Phase C deletes the old `agent`
//! module, the re-exports collapse into native definitions here.

// ---------------------------------------------------------------------------
// Re-exports of pre-existing types that already cross the boundary.
// ---------------------------------------------------------------------------

pub use crate::agent::primitives::community_summary::{
    CommunitySummaryInput, CommunitySummaryOutput, TopWallet,
};
pub use crate::agent::primitives::wallet_profile::{
    TopCounterparty, WalletProfileInput, WalletProfileOutput,
};
pub use crate::agent::types::{NodeStatsWire, ProvenanceRef, SubgraphSlice, TimeScope};
pub use crate::analytics::roles::NodeRole;

// ---------------------------------------------------------------------------
// New types introduced for Phase A.
// ---------------------------------------------------------------------------

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Response from `POST /turn/begin`. The Python agent stashes the
/// `snapshot_id` and passes it on every subsequent primitive call in
/// the same turn so reads are consistent across primitives even if
/// new blocks ingest mid-turn.
///
/// `expires_at_ms` is a wallclock floor; the actual GC sweep on the
/// Rust side may keep the snapshot slightly longer. After this time
/// the Python client must re-`/turn/begin`.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SnapshotBeginResponse {
    pub snapshot_id: String,
    pub expires_at_ms: u64,
    /// The window the snapshot was materialized at (matches the agent's
    /// live window). Phase A only supports 60s; future ships may add
    /// a `window_secs` field on the begin request to negotiate.
    pub window_secs: u32,
}

/// Body of `POST /turn/end`. Releases the snapshot eagerly. Optional;
/// the GC sweep would clean it up within 5 minutes anyway, but eager
/// release keeps the cache small for healthy turns.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct SnapshotEndRequest {
    pub snapshot_id: String,
}

/// Body of `POST /primitive/wallet_profile`. Wraps the existing
/// `WalletProfileInput` with the snapshot lease so the route is
/// addressable from Python without ambient state.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct WalletProfileRequest {
    pub input: WalletProfileInput,
    pub snapshot_id: String,
}

/// Body of `POST /primitive/community_summary`. Same envelope shape
/// as `WalletProfileRequest`.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct CommunitySummaryRequest {
    pub input: CommunitySummaryInput,
    pub snapshot_id: String,
}

/// Generic primitive response envelope. Mirrors `PrimitiveOutput<T>`
/// from the existing primitive trait but flattened for the wire so
/// Python doesn't need to learn about the Rust generic.
///
/// `value` is intentionally `serde_json::Value` here in the Rust shape
/// so this single envelope can wrap any primitive's output. The
/// Python client typed-validates `value` against the matching
/// per-primitive output model.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct PrimitiveResponseEnvelope {
    pub value: serde_json::Value,
    pub provenance: Vec<ProvenanceRef>,
    pub subgraph_slice: Option<SubgraphSlice>,
}
