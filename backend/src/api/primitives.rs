//! HTTP routes the Python `agent-service` (port 8003) calls into the
//! Rust data plane (port 8002) for primitive computation. Phase 0 of
//! the Python-agent migration: only `wallet_profile` Live arm is wired,
//! no snapshot lease yet (Phase A adds `/turn/begin`, `/turn/end`, and
//! a `snapshot_id` field on every primitive request).
//!
//! Wire shape mirrors the `Primitive` trait: input -> `PrimitiveOutput`
//! (value + provenance + optional subgraph slice). Errors map to HTTP
//! status codes and a JSON body of shape `{ "error": "...", "kind":
//! "..." }` so the Python client can branch on the error class.

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use serde::Serialize;
use serde_json::{Value, json};

use crate::agent::primitives::{wallet_profile, PrimitiveError, PrimitiveOutput};
use crate::agent::types::ProvenanceRef;
use crate::state::AppState;

/// Wire form of `PrimitiveOutput<T>` for Python consumption. Flattens
/// `value`, `provenance`, `subgraph_slice` into one envelope rather
/// than nesting under a generic. The Python `primitive_client` parses
/// `value` against the matching pydantic model per primitive name.
#[derive(Serialize)]
struct PrimitiveResponse<T: Serialize> {
    value: T,
    provenance: Vec<ProvenanceRef>,
    subgraph_slice: Option<crate::agent::types::SubgraphSlice>,
}

impl<T: Serialize> From<PrimitiveOutput<T>> for PrimitiveResponse<T> {
    fn from(out: PrimitiveOutput<T>) -> Self {
        Self {
            value: out.value,
            provenance: out.provenance,
            subgraph_slice: out.subgraph_slice,
        }
    }
}

/// Map `PrimitiveError` to an HTTP status + JSON body. The Python side
/// branches on `kind` to decide whether to retry, surface to the model,
/// or kill the turn.
fn error_response(err: PrimitiveError) -> (StatusCode, Json<Value>) {
    let (status, kind) = match &err {
        PrimitiveError::InvalidInput { .. } => (StatusCode::BAD_REQUEST, "invalid_input"),
        PrimitiveError::NotInWindow { .. } => (StatusCode::NOT_FOUND, "not_in_window"),
        PrimitiveError::NotImplemented { .. } => (StatusCode::NOT_IMPLEMENTED, "not_implemented"),
        PrimitiveError::Internal(_) => (StatusCode::INTERNAL_SERVER_ERROR, "internal"),
    };
    (
        status,
        Json(json!({
            "error": err.to_string(),
            "kind": kind,
        })),
    )
}

pub async fn wallet_profile_route(
    State(state): State<AppState>,
    Json(input): Json<wallet_profile::WalletProfileInput>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    match wallet_profile::compute(&state, input).await {
        Ok(out) => {
            let resp: PrimitiveResponse<wallet_profile::WalletProfileOutput> = out.into();
            let body = serde_json::to_value(&resp).map_err(|e| {
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({ "error": format!("serialize: {e}"), "kind": "internal" })),
                )
            })?;
            Ok(Json(body))
        }
        Err(err) => Err(error_response(err)),
    }
}
