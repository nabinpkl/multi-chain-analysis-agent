pub mod graph_stats;
pub mod graph_stream;
pub mod health;

use axum::Router;
use axum::routing::get;

use crate::state::AppState;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/stream", get(graph_stream::stream))
        .route("/graph/stats", get(graph_stats::stats))
        .with_state(state)
}
