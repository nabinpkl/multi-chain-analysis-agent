//! Internal Rust shapes the primitive compute functions consume and
//! produce. Bridged to/from the proto wire types in `crate::wire::proto_bridge`.
//!
//! Why these stay hand-rolled instead of using proto types directly:
//! the compute fns predate the proto migration. Migrating the compute
//! fns onto proto types is a clean refactor (no logic change), but
//! out of scope for Phase C. The bridge does the conversion in one
//! place; nothing else in the data plane sees these types.
//!
//! Removed in Phase C: every type that was specific to the dying
//! agent loop (`Claim`, `NarrativeWithRefs`, `PolicyVerdict`,
//! `GatePath`, etc.). What survived is the minimal surface the
//! compute fns and the bridge mutually need.

use serde::{Deserialize, Serialize};

/// Mandatory temporal frame for primitives. Externally tagged so
/// `Live` serializes as the bare string `"live"` and `Range` as
/// `{"range": {"from_s": ..., "to_s": ...}}`. The proto
/// `TimeScope` oneof bridges to this shape.
#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(rename_all = "kebab-case")]
pub enum TimeScope {
    Live,
    Range { from_s: u32, to_s: u32 },
}

/// Primitive data source family. Carried on `PrimitiveOutput` for
/// trace metadata; not load-bearing in the compute path.
#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum DataSource {
    Live,
    Warehouse,
    External,
}

/// Cost-class tag. Reserved for future budget-gate work; not used in
/// the data plane today.
#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum CostClass {
    Cheap,
    Moderate,
    Expensive,
}

/// Tagged reference back to a graph entity. Bridged to/from the proto
/// `ProvenanceRef` oneof in `wire/proto_bridge.rs`. Field serialization
/// uses the legacy kebab-case tag form because the bridge's tests
/// pin the shape; future cleanup can move to camelCase if useful.
#[derive(Serialize, Deserialize, Debug, Clone)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum ProvenanceRef {
    Wallet { addr: String, idx: Option<u32> },
    Edge { id: String, src: u32, dst: u32 },
    Community { id: u32 },
    TimeRange { from_s: u32, to_s: u32 },
    Number {
        metric: String,
        value: f64,
        support: Vec<String>,
    },
}

/// Aggregate metric reference. Used inline in `support_numbers` arrays
/// the wallet/community primitives emit; bridged to proto `NumberRef`.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct NumberRef {
    pub metric: String,
    pub value: f64,
}

/// Self-contained subgraph rendered on its own canvas in a modal.
/// Reserved for ship-5 warehouse primitives; primitives today emit
/// `subgraph_slice: None`. Bridged through the envelope all the same.
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SubgraphSlice {
    pub nodes: Vec<NodeSummary>,
    pub edges: Vec<EdgeSummary>,
    pub time_range: Option<TimeRangeWire>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct NodeSummary {
    pub addr: String,
    pub role: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct EdgeSummary {
    pub src: String,
    pub dst: String,
    pub volume: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone, Copy)]
pub struct TimeRangeWire {
    pub from_s: u32,
    pub to_s: u32,
}

/// Wire-friendly mirror of `analytics::snapshot::NodeStats`. Field
/// names use the descriptive `*_volume_lamports` form so Python's
/// `policy.crosscheck.classify_metric` resolves them to `UnitClass.SOL`
/// (substring match on "volume" + "lamport"). Internal short keys
/// (`in_vol`/`out_vol`/`bidir_vol`) get renamed here.
#[derive(Serialize, Deserialize, Debug, Clone, Copy, Default)]
pub struct NodeStatsWire {
    pub degree: u32,
    pub total_volume_lamports: f64,
    pub in_volume_lamports: f64,
    pub out_volume_lamports: f64,
    pub bidir_volume_lamports: f64,
    pub sol_degree: u32,
    pub spl_degree: u32,
}

impl From<&crate::analytics::snapshot::NodeStats> for NodeStatsWire {
    fn from(s: &crate::analytics::snapshot::NodeStats) -> Self {
        Self {
            degree: s.degree,
            total_volume_lamports: s.volume,
            in_volume_lamports: s.in_vol,
            out_volume_lamports: s.out_vol,
            bidir_volume_lamports: s.bidir_vol,
            sol_degree: s.sol_degree,
            spl_degree: s.spl_degree,
        }
    }
}
