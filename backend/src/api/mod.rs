pub mod graph;
pub mod health;
pub mod stream;

use axum::Router;
use axum::routing::get;

use crate::state::AppState;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/overview", get(graph::overview))
        .route("/graph/overview/stream", get(stream::stream))
        .with_state(state)
}
