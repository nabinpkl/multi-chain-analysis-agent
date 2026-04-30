pub mod agent;
pub mod diagnostics;
pub mod graph_stats;
pub mod graph_stream;
pub mod health;

use axum::Router;
use axum::routing::{get, post};

use crate::state::AppState;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/stream", get(graph_stream::stream))
        .route("/graph/stats", get(graph_stats::stats))
        .route("/agent/ask", post(agent::ask))
        .route("/agent/stream/{session_id}", get(agent::stream))
        .route("/agent/diagnostics", get(diagnostics::diagnostics))
        .with_state(state)
}
