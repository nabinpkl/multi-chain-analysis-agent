pub mod graph_stats;
pub mod graph_stream;
pub mod health;
pub mod observable_wallets;
pub mod primitives;

use axum::Router;
use axum::routing::{get, post};
use rmcp::transport::streamable_http_server::{
    StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
};

use crate::mcp::McaeMcp;
use crate::state::AppState;

/// Public HTTP surface. Browser-reachable from whatever ingress sits
/// in front of this listener. Only routes safe for unauthenticated
/// external callers live here: liveness checks and the read-only
/// graph stream + stats. Bound to the `PORT` env var (default 8002)
/// and host-published in docker-compose.
pub fn public_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/graph/stream", get(graph_stream::stream))
        .route("/graph/stats", get(graph_stats::stats))
        // Returns the top-degree wallets currently visible in the
        // live window. Read-only. Intended for the eval harness so
        // suite cases that need a "currently observable" wallet can
        // resolve one on demand instead of pinning an address that
        // ages out of the window. Safe for the public router because
        // it surfaces only pubkeys and window-local degree counts
        // (same shape `/graph/stats` already exposes in aggregate).
        .route(
            "/graph/observable_wallets",
            get(observable_wallets::observable_wallets),
        )
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
    // Streamable-HTTP MCP service mounted at `/mcp` on the same
    // internal listener. Tool surface lives in `crate::mcp`. Reuses
    // `AppState` (cheap clone, every field is Arc / channel-handle
    // internally) so MCP tool fns call the same business-logic fns
    // the existing `/primitive/*` HTTP handlers wrap, in-process,
    // with zero serialization overhead. The factory closure runs
    // per MCP session so concurrent codex turns each carry their own
    // session id without sharing tool-router state.
    let mcp_state = state.clone();
    // rmcp ships a default Host-header allowlist as a DNS-rebind
    // defense for browser-reachable MCP endpoints. Our listener is
    // docker-network-only today, but we keep the safeguard live so
    // the day this surface ever moves to a browser-reachable
    // listener the defense is already in place rather than
    // rediscovered after an incident. Allowlist sources from
    // `MCP_ALLOWED_HOSTS` env (default
    // `localhost,127.0.0.1,::1,api`); ops can extend without a
    // recompile by editing the env. Entries without a port match
    // any port; entries with a port match only that port.
    let mcp_allowed_hosts = state.mcp_allowed_hosts.clone();
    let mcp_service = StreamableHttpService::new(
        move || Ok(McaeMcp::new(mcp_state.clone())),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default().with_allowed_hosts(mcp_allowed_hosts),
    );

    Router::new()
        .route("/health", get(health::health))
        .route("/ready", get(health::ready))
        .route("/turn/begin", post(primitives::turn_begin))
        .route("/turn/end", post(primitives::turn_end))
        // SSE drain for the codex-path emit_claims channel. Single
        // consumer per snapshot enforced inside the handler. Lives
        // alongside /turn/{begin,end} because it's the same per-turn
        // lifecycle surface.
        .route(
            "/turn/{snapshot_id}/claims",
            get(primitives::stream_claims),
        )
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
        .nest_service("/mcp", mcp_service)
        .with_state(state)
}
