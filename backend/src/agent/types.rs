//! Wire types shared between agent backend and frontend. All exported via
//! ts-rs to `frontend/src/lib/generated/` so the boundary is type-safe.
//!
//! These are the locked-in wire shapes per the ship-1 plan. Future
//! ships consume them as additions only, never modifications.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use ts_rs::TS;

// ============================================================================
// Agent request / context
// ============================================================================

/// User question + structured ground-truth context the frontend builds
/// from its own DOM/state. Per D-6 (overview), the context block is the
/// strongest disambiguation signal.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentRequest {
    pub user_question: String,
    pub context: ViewContext,
}

#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ViewContext {
    pub live_window_secs: u32,
    pub focus: Option<EntityRef>,
    pub selection: Vec<EntityRef>,
}

#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "kind", content = "id", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum EntityRef {
    Wallet(String),
    Edge(String),
    Community(u32),
}

#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentSessionStarted {
    pub session_id: String,
}

/// Final SSE event for a session. `elapsed_ms` is u32 (caps at ~50d,
/// plenty) to dodge the bigint serialization headache.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentDone {
    pub session_id: String,
    pub elapsed_ms: u32,
}

// ============================================================================
// Claim wire format (locked, per ship-1 plan)
// ============================================================================

/// Streamed analytical statement. The body uses `${ref:N}` placeholders
/// the frontend replaces with interactive chips at render time.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct Claim {
    /// ULID, sortable by emission order.
    pub id: String,
    pub session_id: String,
    pub kind: ClaimKind,
    /// 1-line plaintext (already escaped).
    pub headline: String,
    /// Structured paragraph; `${ref:N}` placeholders -> provenance chips.
    pub body_markdown: String,
    pub provenance: Vec<ProvenanceRef>,
    pub support_numbers: Vec<NumberRef>,
    /// None in v0; ship 5 populates for warehouse-derived historical.
    pub subgraph_slice: Option<SubgraphSlice>,
    /// Approved in v0 (stub policy). Ship 2 produces Retracted.
    pub policy_verdict: PolicyVerdict,
    /// Stubs that touched this claim's emission. Visible in the UI as
    /// "via stubs: ..." so historical claims keep their provenance even
    /// after stubs are removed.
    pub stubs_active: Vec<StubMarker>,
    /// Wallclock ms since session started. u32 to dodge bigint.
    pub emitted_at_ms: u32,
}

/// Closed enum: the renderer dispatches to per-kind cards. New variants
/// require a deliberate change. v0 only emits `Profile`; the rest exist
/// so ships 3/5/7 fill them without later refactor.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum ClaimKind {
    Profile,
    Pattern,
    Comparison,
    Summary,
    Pulse,
}

/// Tagged reference back to a graph entity. The frontend's render-surface
/// derivation picks live highlight, modal, or inline chip based on the
/// ref shape (see plan: "Frontend render-surface derivation rule").
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[serde(tag = "kind", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum ProvenanceRef {
    /// `idx` is None when the wallet is not in the current live window
    /// (route to subgraph modal instead of live-graph chip).
    Wallet { addr: String, idx: Option<u32> },
    /// Stable id format: `"<edge_idx>:<gen>"`.
    Edge { id: String, src: u32, dst: u32 },
    Community { id: u32 },
    /// Populated by ship-5 warehouse primitives.
    TimeRange { from_s: u32, to_s: u32 },
    /// Aggregate metric reference. `support` lists EdgeIds backing the
    /// number so the user can drill in.
    Number {
        metric: String,
        value: f64,
        support: Vec<String>,
    },
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NumberRef {
    pub metric: String,
    pub value: f64,
}

/// Self-contained subgraph rendered on its own canvas in a modal.
/// Used by ship-5 for historical results that don't share layout
/// state with the live graph.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct SubgraphSlice {
    pub nodes: Vec<NodeSummary>,
    pub edges: Vec<EdgeSummary>,
    pub time_range: Option<TimeRangeWire>,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NodeSummary {
    pub addr: String,
    pub role: Option<String>,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct EdgeSummary {
    pub src: String,
    pub dst: String,
    pub volume: f64,
}

#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct TimeRangeWire {
    pub from_s: u32,
    pub to_s: u32,
}

/// Output-policy verdict (phase 03 layer 3). v0 is always Approved
/// because the policy gate is stubbed; ship 2 starts producing
/// `Retracted`.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[serde(tag = "verdict", rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum PolicyVerdict {
    Approved,
    Retracted { reason: String },
}

// ============================================================================
// Stub visibility (ship-1 first-class foundation)
// ============================================================================

/// Per-claim badge: which stubs short-circuited during this claim's
/// emission. Persists into the claim history so stub provenance is
/// not lost when the global registry is later cleared.
#[derive(Serialize, Deserialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct StubMarker {
    pub name: String,
    pub reason: String,
    pub promoted_in_ship: u8,
}

// ============================================================================
// Temporal + cost-class taxonomy
// ============================================================================

/// Mandatory temporal frame for primitives that have one (per D-6).
/// Externally tagged so `Live` serializes as the bare string `"live"`
/// (which is what models naturally produce for unit variants) and
/// `Range` as `{"range": {"from_s": ..., "to_s": ...}}`. The
/// internally-tagged form was tried first and the model misinterpreted
/// the schema, sending strings where objects were expected.
#[derive(Serialize, Deserialize, TS, JsonSchema, Debug, Clone)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum TimeScope {
    /// Current rolling live window.
    Live,
    /// Absolute block-time range. Routes to warehouse path (ship-5).
    Range { from_s: u32, to_s: u32 },
}

/// Primitive data source family per D-5.
#[derive(Serialize, Deserialize, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum DataSource {
    Live,
    Warehouse,
    External,
}

/// Cost-class tag on each primitive. Ship-4's budget gate reads this.
#[derive(Serialize, Deserialize, TS, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum CostClass {
    Cheap,
    Moderate,
    Expensive,
}

/// Wire-friendly mirror of `analytics::snapshot::NodeStats`. Defined
/// here (rather than ts-rs-deriving the analytics type) to keep the
/// agent's wire shapes self-contained.
#[derive(Serialize, Deserialize, JsonSchema, TS, Debug, Clone, Copy, Default)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct NodeStatsWire {
    pub degree: u32,
    pub volume: f64,
    pub in_vol: f64,
    pub out_vol: f64,
    pub bidir_vol: f64,
    pub sol_degree: u32,
    pub spl_degree: u32,
}

impl From<&crate::analytics::snapshot::NodeStats> for NodeStatsWire {
    fn from(s: &crate::analytics::snapshot::NodeStats) -> Self {
        Self {
            degree: s.degree,
            volume: s.volume,
            in_vol: s.in_vol,
            out_vol: s.out_vol,
            bidir_vol: s.bidir_vol,
            sol_degree: s.sol_degree,
            spl_degree: s.spl_degree,
        }
    }
}
