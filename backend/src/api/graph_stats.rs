use axum::Json;
use axum::extract::State;
use serde::Serialize;
use ts_rs::TS;

use crate::state::AppState;

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct GraphStatsResponse {
    pub total_nodes: u32,
    pub total_edges: u32,
    pub total_components: u32,
    pub largest_component_size: u32,
    #[ts(optional)]
    pub last_ingested_slot: Option<u64>,
}

pub async fn stats(State(state): State<AppState>) -> Json<GraphStatsResponse> {
    let graph = state.graph.read();
    Json(GraphStatsResponse {
        total_nodes: graph.total_nodes(),
        total_edges: graph.total_edges(),
        total_components: graph.total_components(),
        largest_component_size: graph.largest_component_size(),
        last_ingested_slot: graph.last_ingested_slot(),
    })
}
