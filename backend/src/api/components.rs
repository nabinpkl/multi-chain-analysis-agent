use axum::Json;
use axum::extract::{Query, State};
use serde::{Deserialize, Serialize};
use ts_rs::TS;

use crate::state::AppState;

#[derive(Deserialize)]
pub struct ComponentsQuery {
    #[serde(default = "default_limit")]
    limit: u32,
    #[serde(default)]
    cursor: Option<u32>,
}

fn default_limit() -> u32 {
    5000
}

const HARD_CAP: u32 = 50_000;

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ComponentNode {
    pub pubkey: String,
    pub component_id: u32,
}

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct ComponentsResponse {
    pub nodes: Vec<ComponentNode>,
    #[ts(optional)]
    pub next_cursor: Option<u32>,
    pub total_nodes: u32,
    pub total_components: u32,
}

pub async fn query(
    State(state): State<AppState>,
    Query(q): Query<ComponentsQuery>,
) -> Json<ComponentsResponse> {
    let limit = q.limit.min(HARD_CAP);
    let start = q.cursor.unwrap_or(0);

    let mut graph = state.graph.write();

    let total_nodes = graph.total_nodes();
    let total_components = graph.total_components();
    let pairs = graph.iter_nodes_from(start, limit);
    let fetched = pairs.len() as u32;

    let nodes = pairs
        .into_iter()
        .map(|(pubkey, component_id)| ComponentNode { pubkey, component_id })
        .collect();

    let next_cursor = if start + fetched < total_nodes {
        Some(start + fetched)
    } else {
        None
    };

    Json(ComponentsResponse {
        nodes,
        next_cursor,
        total_nodes,
        total_components,
    })
}
