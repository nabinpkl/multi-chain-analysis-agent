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
        idx: u32,
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
    },
    NodeExpired {
        seq: u64,
        idx: u32,
    },
    PositionsBatch {
        seq: u64,
        positions: Vec<PositionUpdate>,
    },
    CaughtUp {
        seq: u64,
    },
}

#[derive(Serialize, TS, Clone, Debug)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct PositionUpdate {
    pub idx: u32,
    pub x: f32,
    pub y: f32,
}

impl GraphDelta {
    pub fn seq(&self) -> u64 {
        match self {
            GraphDelta::NodeAdded { seq, .. } => *seq,
            GraphDelta::EdgeAdded { seq, .. } => *seq,
            GraphDelta::ComponentAssigned { seq, .. } => *seq,
            GraphDelta::EdgeExpired { seq, .. } => *seq,
            GraphDelta::NodeExpired { seq, .. } => *seq,
            GraphDelta::PositionsBatch { seq, .. } => *seq,
            GraphDelta::CaughtUp { seq } => *seq,
        }
    }

    /// Produce a bootstrap copy with seq=0 (used during cold-start where
    /// events are not tagged with live sequence numbers).
    pub fn with_seq_zero(self) -> Self {
        match self {
            GraphDelta::NodeAdded { idx, pubkey, .. } => {
                GraphDelta::NodeAdded { seq: 0, idx, pubkey }
            }
            GraphDelta::EdgeAdded { idx, src, dst, mint, amount, slot, kind, .. } => {
                GraphDelta::EdgeAdded { seq: 0, idx, src, dst, mint, amount, slot, kind }
            }
            GraphDelta::ComponentAssigned { node, component_id, .. } => {
                GraphDelta::ComponentAssigned { seq: 0, node, component_id }
            }
            GraphDelta::PositionsBatch { positions, .. } => {
                GraphDelta::PositionsBatch { seq: 0, positions }
            }
            other => other,
        }
    }
}
