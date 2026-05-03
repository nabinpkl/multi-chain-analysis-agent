//! HTTP routes the Python `agent-service` (port 8003) calls into the
//! Rust data plane (port 8002) for primitive computation. Phase A of
//! the Python-agent migration introduces the snapshot lease so the
//! Python orchestrator can hold a consistent view across multiple
//! primitive calls in one turn.
//!
//! Routes:
//! - `POST /turn/begin` -> `SnapshotBeginResponse { snapshot_id, expires_at_ms, window_secs }`
//! - `POST /turn/end`   body `SnapshotEndRequest { snapshot_id }` -> 204
//! - `POST /primitive/wallet_profile`    body `WalletProfileRequest`
//! - `POST /primitive/community_summary` body `CommunitySummaryRequest`
//!
//! Errors map to HTTP status codes and a JSON body of shape
//! `{ "error": "...", "kind": "..." }` so the Python client can
//! branch on the error class. Snapshot-not-found is `410 Gone` so the
//! Python client can retry `/turn/begin`.

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use serde::Serialize;
use serde_json::{Value, json};

use crate::agent::primitives::{community_summary, wallet_profile, PrimitiveError, PrimitiveOutput};
use crate::agent::snapshot::{TurnSnapshot, current_time_ms};
use crate::agent::types::ProvenanceRef;
use crate::graph::window::window_index;
use crate::state::AppState;
use crate::wire::shared::{
    CommunitySummaryRequest, SnapshotBeginResponse, SnapshotEndRequest, WalletProfileRequest,
};

/// Wire form of `PrimitiveOutput<T>` for Python consumption. Flattens
/// `value`, `provenance`, `subgraph_slice` into one envelope rather
/// than nesting under a generic.
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
/// branches on `kind` to decide whether to retry, surface to the
/// model, or kill the turn.
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

/// Snapshot-not-found maps to 410 Gone so the Python client knows to
/// retry `/turn/begin` rather than treat it as a transient 5xx.
fn snapshot_gone(snapshot_id: &str) -> (StatusCode, Json<Value>) {
    (
        StatusCode::GONE,
        Json(json!({
            "error": format!("snapshot_id {snapshot_id} not found or expired"),
            "kind": "snapshot_gone",
        })),
    )
}

/// `POST /turn/begin`. Materializes a `TurnSnapshot` against the live
/// 60s window, stashes it in the cache under a fresh `snapshot_id`
/// (ulid for sortability), returns the lease descriptor.
pub async fn turn_begin(
    State(state): State<AppState>,
) -> Result<Json<SnapshotBeginResponse>, (StatusCode, Json<Value>)> {
    let live_window_idx = window_index(60).unwrap_or(1);
    let analytics = state
        .analytics
        .snapshots[live_window_idx]
        .borrow()
        .clone();

    let snapshot_id = ulid::Ulid::new().to_string();
    let now_ms = current_time_ms();
    let snap = TurnSnapshot::build(
        snapshot_id.clone(),
        live_window_idx,
        60,
        now_ms,
        &state.graph,
        analytics,
    );
    let expires_at_ms = snap.expires_at_ms;
    state.snapshot_cache.insert(snap);

    Ok(Json(SnapshotBeginResponse {
        snapshot_id,
        expires_at_ms,
        window_secs: 60,
    }))
}

/// `POST /turn/end`. Idempotent. Always 204 even if the snapshot was
/// already gone (GC sweep, double-end, etc). Python should fire-and-
/// forget this in a `finally` block and ignore the response.
pub async fn turn_end(
    State(state): State<AppState>,
    Json(body): Json<SnapshotEndRequest>,
) -> StatusCode {
    state.snapshot_cache.remove(&body.snapshot_id);
    StatusCode::NO_CONTENT
}

pub async fn wallet_profile_route(
    State(state): State<AppState>,
    Json(req): Json<WalletProfileRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let snapshot = match state.snapshot_cache.get(&req.snapshot_id) {
        Some(s) => s,
        None => return Err(snapshot_gone(&req.snapshot_id)),
    };
    match wallet_profile::compute_with_snapshot(&state, &snapshot, req.input).await {
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

pub async fn community_summary_route(
    State(state): State<AppState>,
    Json(req): Json<CommunitySummaryRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let snapshot = match state.snapshot_cache.get(&req.snapshot_id) {
        Some(s) => s,
        None => return Err(snapshot_gone(&req.snapshot_id)),
    };
    match community_summary::compute_with_snapshot(&state, &snapshot, req.input).await {
        Ok(out) => {
            let resp: PrimitiveResponse<community_summary::CommunitySummaryOutput> = out.into();
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

/// Re-export for the router. Avoids leaking individual handler names
/// outside this module.
pub fn _placate_unused_import(_: impl IntoResponse) {}
