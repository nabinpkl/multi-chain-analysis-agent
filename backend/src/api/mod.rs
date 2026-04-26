pub mod health;
pub mod raw;

use axum::Router;
use axum::routing::get;

use crate::state::AppState;

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/raw/stream", get(raw::stream))
        .with_state(state)
}
