pub mod graph_stats;
pub mod graph_stream;
pub mod health;
pub mod primitives;

use axum::Router;
use axum::routing::{get, post};

use crate::state::AppState;

/// Public HTTP surface. Browser-reachable via the cloudflared tunnel.
/// Only routes safe for unauthenticated external callers live here:
/// liveness checks and the read-only graph stream + stats. Bound to
/// the `PORT` env var (default 8002) and host-published in
/// docker-compose.
pub fn public_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/stream", get(graph_stream::stream))
        .route("/graph/stats", get(graph_stats::stats))
        .with_state(state)
}

/// Internal HTTP surface. Carries the snapshot-lease + primitive
/// routes the Python agent on `:8003` calls. Bound to the
/// `INTERNAL_PORT` env var (default 8004) and NOT host-published in
/// docker-compose, so the only path to it is the docker compose
/// network. Browsers and external callers cannot reach it.
///
/// No CORS layer attached: there is no browser caller, ever.
///
/// `/health` and `/ready` are mounted on both listeners so any
/// docker-network sibling (the agent-service container, an integration
/// test running inside the compose network, etc.) can liveness-check
/// the internal port without cross-port plumbing.
pub fn internal_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
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
        .route(
            "/primitive/get_token_info",
            post(primitives::get_token_info_route),
        )
        .with_state(state)
}
