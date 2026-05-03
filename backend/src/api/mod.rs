pub mod graph_stats;
pub mod graph_stream;
pub mod health;
pub mod primitives;

use axum::Router;
use axum::routing::{get, post};

use crate::state::AppState;

/// HTTP surface of the data plane. Phase C deleted the `/agent/*`
/// routes; the Python agent service on `:8003` owns the agent plane
/// end-to-end. The data plane keeps the graph + primitive endpoints
/// the Python orchestrator and the frontend both call into.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/stream", get(graph_stream::stream))
        .route("/graph/stats", get(graph_stats::stats))
        // Primitive surface for the Python agent orchestrator.
        // Snapshot lease pins a consistent view across multiple
        // primitive calls in one turn.
        .route("/turn/begin", post(primitives::turn_begin))
        .route("/turn/end", post(primitives::turn_end))
        .route(
            "/primitive/wallet_profile",
            post(primitives::wallet_profile_route),
        )
        .route(
            "/primitive/community_summary",
            post(primitives::community_summary_route),
        )
        .with_state(state)
}
