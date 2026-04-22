//! State deltas emitted on each transition — placeholder for Phase 4 SSE.
//!
//! In Phase 3 the state machine doesn't emit deltas yet; the API reads
//! from RAM on demand. Phase 4 will wire broadcast emission from `apply`.

use serde::Serialize;

use crate::domain::{EdgeView, NodeView, StatsView};

#[derive(Debug, Clone, Serialize)]
pub struct StateDelta {
    pub seq: u64,
    pub generated_at: u32,
    pub effective_label: String,
    pub is_partial: bool,
    pub stats: StatsView,
    pub nodes_added: Vec<NodeView>,
    pub nodes_removed: Vec<String>,
    pub edges_added: Vec<EdgeView>,
    pub edges_removed: Vec<(String, String)>,
    pub components: Option<Vec<(String, u32)>>,
}
