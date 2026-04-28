use std::collections::HashMap;

use axum::Json;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use rustc_hash::{FxHashMap, FxHashSet};
use serde::Serialize;
use ts_rs::TS;

use crate::graph::interner::NodeIdx;
use crate::graph::window::parse_window_param;
use crate::state::AppState;

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct GraphStatsResponse {
    /// Window the response was computed for, in seconds.
    pub window_secs: u64,
    pub total_nodes: u32,
    pub total_edges: u32,
    pub total_components: u32,
    pub largest_component_size: u32,
    #[ts(optional)]
    pub last_ingested_slot: Option<u64>,
    /// Tip of the ingest stream, in `block_time` (Unix seconds). The
    /// cutoff applied for this response was `latest_block_time -
    /// window_secs` (or 0 for the global window).
    pub latest_block_time: u64,
    /// `block_time` span, in seconds, between the oldest and newest live
    /// edge in the global slab. Grows from 0 toward `WINDOWS[MAX]`
    /// (3600s) as the rolling buffer fills, then stays pinned at it.
    pub accumulated_secs: u64,
}

pub async fn stats(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let window_idx = match parse_window_param(params.get("window").map(|s| s.as_str())) {
        Ok(w) => w,
        Err(msg) => return (StatusCode::BAD_REQUEST, msg).into_response(),
    };
    let window_secs = crate::graph::window::WINDOWS[window_idx];

    let graph = state.graph.read();

    // Walk live edges restricted to the window. Nodes/components are
    // derived from edge endpoints; a node with zero edges in the window
    // is invisible regardless of whether it exists globally.
    let cutoff = if window_idx == crate::graph::window::MAX_WINDOW_IDX {
        0
    } else {
        graph.latest_block_time().saturating_sub(window_secs)
    };

    let mut visible_nodes: FxHashSet<NodeIdx> = FxHashSet::default();
    let mut total_edges: u32 = 0;
    let mut oldest_block_time: Option<u64> = None;
    for slot in graph.edges.iter() {
        let Some(e) = slot.edge.as_ref() else { continue };
        oldest_block_time = Some(match oldest_block_time {
            Some(prev) => prev.min(e.block_time),
            None => e.block_time,
        });
        if e.block_time < cutoff {
            continue;
        }
        total_edges += 1;
        visible_nodes.insert(e.src);
        visible_nodes.insert(e.dst);
    }
    let accumulated_secs = oldest_block_time
        .map(|oldest| graph.latest_block_time().saturating_sub(oldest))
        .unwrap_or(0);

    let mut component_counts: FxHashMap<u64, u32> = FxHashMap::default();
    for &n in &visible_nodes {
        let cid = graph
            .node_to_component
            .get(n as usize)
            .copied()
            .unwrap_or(u64::MAX);
        if cid == u64::MAX {
            continue;
        }
        *component_counts.entry(cid).or_insert(0) += 1;
    }

    Json(GraphStatsResponse {
        window_secs,
        total_nodes: visible_nodes.len() as u32,
        total_edges,
        total_components: component_counts.len() as u32,
        largest_component_size: component_counts.values().copied().max().unwrap_or(0),
        last_ingested_slot: graph.last_ingested_slot(),
        latest_block_time: graph.latest_block_time(),
        accumulated_secs,
    })
    .into_response()
}
