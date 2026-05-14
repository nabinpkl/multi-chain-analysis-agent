//! HTTP routes for the eval-fixture store. Used by the Python agent
//! service to inject canned `get_token_info` responses for an
//! adversarial-mint eval case, then clear them at the end of the turn.
//! See `crate::eval_fixtures` for the underlying store contract.
//!
//! Both routes are guarded by `state.eval_fixtures_enabled` (sourced
//! from `BACKEND_ENABLE_EVAL_FIXTURES` env). Production deploys leave
//! the flag off so the surface is unreachable; the docker compose
//! eval profile flips it on.

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use serde::Serialize;
use serde_json::{Value, json};

use crate::eval_fixtures::{self, RegisterRequest};
use crate::state::AppState;

#[derive(Debug, Serialize)]
struct RegisterResponse {
    count: usize,
}

/// `POST /eval/fixtures`. Body is `RegisterRequest` JSON; replaces
/// the live store contents. Returns 200 with `{"count": N}` on
/// success, 400 on validation failure (empty mint, fixture targets
/// a canonical mint pubkey), 503 when the feature flag is off.
pub async fn register(
    State(state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> (StatusCode, Json<Value>) {
    match eval_fixtures::replace(&state, req) {
        Ok(count) => (
            StatusCode::OK,
            Json(json!(RegisterResponse { count })),
        ),
        Err(msg) if msg.contains("disabled") => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": msg, "kind": "feature_disabled" })),
        ),
        Err(msg) => (
            StatusCode::BAD_REQUEST,
            Json(json!({ "error": msg, "kind": "invalid_fixture" })),
        ),
    }
}

/// `DELETE /eval/fixtures`. Clears the live store. Always succeeds
/// when the feature flag is on (clearing an already-empty store is a
/// no-op); 503 when off.
pub async fn clear(State(state): State<AppState>) -> (StatusCode, Json<Value>) {
    match eval_fixtures::clear(&state) {
        Ok(()) => (StatusCode::NO_CONTENT, Json(json!({}))),
        Err(msg) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "error": msg, "kind": "feature_disabled" })),
        ),
    }
}
