use serde::Serialize;
use ts_rs::TS;

/// Token-issuance / destruction direction on the wire. Only present for
/// SPL edges that originate from or terminate at a mint authority.
#[derive(Serialize, TS, Clone, Debug)]
#[serde(rename_all = "lowercase")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum EdgeKind {
    Mint,
    Burn,
}

#[derive(Serialize, TS, Clone, Debug)]
#[serde(tag = "type")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub enum GraphDelta {
    NodeAdded {
        seq: u64,
        idx: u32,
        pubkey: String,
    },
    EdgeAdded {
        seq: u64,
        /// Slab slot index. Together with `gen`, uniquely identifies
        /// a single edge across all reuses of the slot. Frontend keys
        /// graphology by `${idx}:${gen}`.
        idx: u32,
        /// Slab slot generation tag, bumped on every reuse. Wire name
        /// is `gen`; the Rust field is renamed because `gen` is a
        /// reserved keyword in Rust 2024.
        #[serde(rename = "gen")]
        #[ts(rename = "gen")]
        generation: u32,
        src: u32,
        dst: u32,
        mint: Option<String>,
        amount: u64,
        slot: u64,
        kind: Option<EdgeKind>,
    },
    ComponentAssigned {
        seq: u64,
        node: u32,
        component_id: u64,
    },
    EdgeExpired {
        seq: u64,
        idx: u32,
        /// Generation of the edge being expired. Frontend uses
        /// `${idx}:${gen}` to drop the exact graphology entry, so an
        /// expiry for a long-gone generation can't accidentally drop
        /// the current occupant of the same slot.
        #[serde(rename = "gen")]
        #[ts(rename = "gen")]
        generation: u32,
    },
    NodeExpired {
        seq: u64,
        idx: u32,
    },
    CaughtUp {
        seq: u64,
    },
}

impl GraphDelta {
    pub fn seq(&self) -> u64 {
        match self {
            GraphDelta::NodeAdded { seq, .. } => *seq,
            GraphDelta::EdgeAdded { seq, .. } => *seq,
            GraphDelta::ComponentAssigned { seq, .. } => *seq,
            GraphDelta::EdgeExpired { seq, .. } => *seq,
            GraphDelta::NodeExpired { seq, .. } => *seq,
            GraphDelta::CaughtUp { seq } => *seq,
        }
    }
}
