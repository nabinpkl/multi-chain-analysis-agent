//! HTTP routes the Python `agent-service` (port 8003) calls into the
//! Rust data plane (port 8002) for primitive computation. Phase A of
//! the Python-agent migration introduces the snapshot lease so the
//! Python orchestrator can hold a consistent view across multiple
//! primitive calls in one turn. Stage 1 of the proto migration wires
//! every request/response through the proto-generated wire types.
//!
//! Routes:
//! - `POST /turn/begin` -> `proto::SnapshotBeginResponse`
//! - `POST /turn/end`   body `proto::SnapshotEndRequest` -> 204
//! - `POST /primitive/wallet_profile`    body `proto::WalletProfileRequest`
//! - `POST /primitive/community_summary` body `proto::CommunitySummaryRequest`
//!
//! Wire format per the AGENTS.md "Wire format per hop" matrix:
//! - `Content-Type: application/x-protobuf`  binary protobuf (the
//!   primary path, what Python uses in production after Stage 2).
//! - `Content-Type: application/json`        proto canonical JSON
//!   (curl-debuggable fallback). Driven by the same proto types
//!   serialized via the buffa serde impls.
//!
//! Errors map to HTTP status codes; the body shape matches the
//! request's wire format (binary or JSON). Snapshot-not-found is
//! `410 Gone` so the Python client can branch on it and retry
//! `/turn/begin`.

use std::convert::Infallible;

use std::collections::HashMap;

use axum::body::{Body, Bytes};
use axum::extract::{FromRequest, Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::sse::{Event, KeepAlive};
use axum::response::{IntoResponse, Response, Sse};
use futures_util::stream::StreamExt;
use tokio_stream::wrappers::UnboundedReceiverStream;

use buffa::Message;
use serde_json::json;

use crate::primitives::{
    PrimitiveError, PrimitiveOutput, community_summary, wallet_profile,
};
use crate::snapshot::{TurnSnapshot, current_time_ms};
use crate::graph::window::{WINDOWS, window_index};
use crate::state::AppState;
use crate::wire::generated::multichain::wire::shared::v1 as proto;
use crate::wire::proto_bridge::{self, BridgeError};

const CT_PROTOBUF: &str = "application/x-protobuf";
const CT_JSON: &str = "application/json";

// ---------------------------------------------------------------------------
// Wire-format negotiation. Sniff Content-Type once per request; carry
// the choice through to the response so the client gets back what it
// sent. The same enum drives request decode and response encode.
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum WireFormat {
    Protobuf,
    Json,
}

impl WireFormat {
    fn from_headers(headers: &HeaderMap) -> Result<Self, Response> {
        let ct = headers
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or(CT_JSON);
        // strip charset/boundary suffixes ("application/json; charset=utf-8")
        let base = ct.split(';').next().unwrap_or("").trim();
        match base {
            CT_PROTOBUF => Ok(WireFormat::Protobuf),
            CT_JSON => Ok(WireFormat::Json),
            "" => Ok(WireFormat::Json),
            other => Err(unsupported_media_type(other)),
        }
    }

}

fn unsupported_media_type(actual: &str) -> Response {
    let body = format!(
        r#"{{"error":"unsupported Content-Type: {actual}; expected application/x-protobuf or application/json","kind":"unsupported_media_type"}}"#
    );
    (StatusCode::UNSUPPORTED_MEDIA_TYPE, Body::from(body)).into_response()
}

// ---------------------------------------------------------------------------
// Decode + encode helpers. JSON path uses buffa's serde_json integration
// (the `json` feature on buffa + buffa-types we enabled in Cargo.toml).
// ---------------------------------------------------------------------------

async fn read_body(req: Request) -> Result<Bytes, Response> {
    Bytes::from_request(req, &()).await.map_err(|e| {
        let body = format!(
            r#"{{"error":"failed to read request body: {e}","kind":"bad_request"}}"#
        );
        (StatusCode::BAD_REQUEST, Body::from(body)).into_response()
    })
}

fn decode_request<M>(format: WireFormat, body: &Bytes) -> Result<M, Response>
where
    M: Message + serde::de::DeserializeOwned + Default,
{
    match format {
        WireFormat::Protobuf => M::decode_from_slice(body).map_err(|e| {
            error_body(
                StatusCode::BAD_REQUEST,
                "decode_protobuf",
                &format!("decode protobuf: {e}"),
                format,
            )
        }),
        WireFormat::Json => serde_json::from_slice(body).map_err(|e| {
            error_body(
                StatusCode::BAD_REQUEST,
                "decode_json",
                &format!("decode json: {e}"),
                format,
            )
        }),
    }
}

fn encode_response<M>(format: WireFormat, msg: &M, status: StatusCode) -> Response
where
    M: Message + serde::Serialize,
{
    let (body, ct): (Vec<u8>, &str) = match format {
        WireFormat::Protobuf => (msg.encode_to_vec(), CT_PROTOBUF),
        WireFormat::Json => match serde_json::to_vec(msg) {
            Ok(b) => (b, CT_JSON),
            Err(e) => {
                return error_body(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "encode_json",
                    &format!("encode json: {e}"),
                    format,
                );
            }
        },
    };
    let mut resp = Response::new(Body::from(body));
    *resp.status_mut() = status;
    resp.headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(ct));
    resp
}

/// Build an error response in the same wire format as the request. The
/// JSON shape (`{"error":..., "kind":...}`) is what Python's existing
/// `primitive_client.py` already branches on; the protobuf path uses
/// the same shape encoded via canonical JSON for now (no proto
/// `Status` message defined yet  follow-up if Python ever wants typed
/// errors over the wire).
fn error_body(status: StatusCode, kind: &str, message: &str, format: WireFormat) -> Response {
    let payload = json!({ "error": message, "kind": kind });
    let bytes = match format {
        // Even on the protobuf path, errors stay JSON-encoded for
        // shape compat with existing Python client.
        WireFormat::Protobuf | WireFormat::Json => serde_json::to_vec(&payload).unwrap_or_default(),
    };
    let mut resp = Response::new(Body::from(bytes));
    *resp.status_mut() = status;
    resp.headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(CT_JSON));
    let _ = format; // currently identical; reserved for typed proto Status
    resp
}

fn primitive_error_response(err: PrimitiveError, format: WireFormat) -> Response {
    let (status, kind) = match &err {
        PrimitiveError::InvalidInput { .. } => (StatusCode::BAD_REQUEST, "invalid_input"),
        PrimitiveError::NotInWindow { .. } => (StatusCode::NOT_FOUND, "not_in_window"),
        PrimitiveError::NotImplemented { .. } => (StatusCode::NOT_IMPLEMENTED, "not_implemented"),
        PrimitiveError::Internal(_) => (StatusCode::INTERNAL_SERVER_ERROR, "internal"),
    };
    error_body(status, kind, &err.to_string(), format)
}

fn bridge_error_response(err: BridgeError, format: WireFormat) -> Response {
    error_body(StatusCode::BAD_REQUEST, "invalid_input", &err.to_string(), format)
}

fn snapshot_gone(snapshot_id: &str, format: WireFormat) -> Response {
    error_body(
        StatusCode::GONE,
        "snapshot_gone",
        &format!("snapshot_id {snapshot_id} not found or expired"),
        format,
    )
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/// Default live-window seconds for `POST /turn/begin` when the caller
/// doesn't pin one via `?window=N`. 60s is the historical default and
/// what every production caller currently relies on; widening the
/// snapshot is opt-in (eval cases, future operator-facing knob).
const TURN_BEGIN_DEFAULT_WINDOW_SECS: u64 = 60;

/// `POST /turn/begin[?window=N]`. Materializes a `TurnSnapshot` against
/// the requested live window (default 60s), stashes it in the cache
/// under a fresh `snapshot_id` (ulid for sortability), returns the
/// lease descriptor.
///
/// `window` query param must be one of the values in
/// `crate::graph::window::WINDOWS` (`[10, 60, 300, 900, 1800, 3600]`).
/// Unrecognized values return 400 so the caller can't silently get a
/// different window than they asked for. Missing / empty param falls
/// back to the 60s default. The resolved `window_secs` is stamped on
/// `SnapshotBeginResponse.window_secs` so the caller can see exactly
/// what was materialized (round-tripping was always the contract; we
/// just stop ignoring inbound preference).
///
/// Begin requests carry no body, so we don't sniff Content-Type for
/// the request side. Response wire format defaults to JSON unless
/// `Accept: application/x-protobuf` is set, matching how curl users
/// expect to see the lease.
pub async fn turn_begin(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
    headers: HeaderMap,
) -> Response {
    let format = match headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(',').next().unwrap_or("").trim())
    {
        Some(CT_PROTOBUF) => WireFormat::Protobuf,
        _ => WireFormat::Json,
    };

    // Resolve the requested window. Empty / missing falls back to 60s
    // (preserving the historical contract for every caller that
    // doesn't opt in); a present-but-invalid value is rejected so a
    // typo never silently materializes the wrong window.
    let window_secs: u64 = match params.get("window").map(|s| s.as_str()) {
        None | Some("") => TURN_BEGIN_DEFAULT_WINDOW_SECS,
        Some(raw) => match raw.parse::<u64>() {
            Ok(s) if window_index(s).is_some() => s,
            _ => {
                return (
                    StatusCode::BAD_REQUEST,
                    format!(
                        "invalid ?window={raw}; expected one of {:?}",
                        WINDOWS
                    ),
                )
                    .into_response();
            }
        },
    };
    let live_window_idx = window_index(window_secs)
        .expect("window_secs validated above; this is unreachable");
    let analytics = state.analytics.snapshots[live_window_idx].borrow().clone();

    let snapshot_id = ulid::Ulid::new().to_string();
    let now_ms = current_time_ms();
    let snap = TurnSnapshot::build(
        snapshot_id.clone(),
        live_window_idx,
        window_secs as u32,
        now_ms,
        &state.graph,
        analytics,
    );
    let expires_at_ms = snap.expires_at_ms;
    state.snapshot_cache.insert(snap);

    // Pair the new snapshot with an unbounded mpsc that the MCP
    // `emit_claims` tool pushes onto and the SSE drain route at
    // `GET /turn/{snapshot_id}/claims` reads from. The receiver
    // is stashed in a separate map keyed by snapshot_id so the
    // drain route's first request can pick it up; the sender stays
    // in `state.claim_channels` until `turn_end` removes it. Any
    // turn that never spawns a codex sub-loop (i.e. the existing
    // Pydantic AI path that still uses `agent.tool emit_claim`)
    // simply leaves both the sender unused and the receiver
    // unsubscribed; the entries are dropped at `turn_end` regardless.
    let (claim_tx, claim_rx) = tokio::sync::mpsc::unbounded_channel();
    state.claim_channels.insert(snapshot_id.clone(), claim_tx);
    // Park the receiver in a separate one-shot map; the SSE drain
    // route consumes it via remove(). One drain per turn is the
    // contract (the Python loop driver opens exactly one drain
    // stream per codex turn).
    state
        .claim_receivers
        .insert(snapshot_id.clone(), parking_lot::Mutex::new(Some(claim_rx)));

    let resp = proto::SnapshotBeginResponse {
        snapshot_id,
        expires_at_ms,
        window_secs: window_secs as u32,
        ..Default::default()
    };
    encode_response(format, &resp, StatusCode::OK)
}

/// `POST /turn/end`. Idempotent. Always 204 even if the snapshot was
/// already gone (GC sweep, double-end, etc). Python should fire-and-
/// forget this in a `finally` block.
pub async fn turn_end(State(state): State<AppState>, req: Request) -> Response {
    let format = match WireFormat::from_headers(req.headers()) {
        Ok(f) => f,
        Err(e) => return e,
    };
    let body = match read_body(req).await {
        Ok(b) => b,
        Err(e) => return e,
    };
    let req_msg: proto::SnapshotEndRequest = match decode_request(format, &body) {
        Ok(m) => m,
        Err(e) => return e,
    };
    state.snapshot_cache.remove(&req_msg.snapshot_id);
    // Drop the claim sender (if present); any active drain on the
    // SSE side sees end-of-stream the next read. Drop the receiver
    // map entry too in case the drain was never opened (e.g. a
    // Pydantic AI primary turn that doesn't use `emit_claims`).
    state.claim_channels.remove(&req_msg.snapshot_id);
    state.claim_receivers.remove(&req_msg.snapshot_id);
    StatusCode::NO_CONTENT.into_response()
}

/// `GET /turn/{snapshot_id}/claims`  SSE drain stream for the
/// codex-path claim channel created at `turn_begin`. Single
/// consumer per snapshot: the receiver is `take()`-n out of
/// `claim_receivers` on first subscribe, so a second subscribe
/// returns 409 Conflict.
///
/// Frame shape:
///
/// ```text
/// event: claim
/// data: {"kind":"PROFILE","headline":"...","body_markdown":"...",
///        "provenance":[...], "support_numbers":[...]}
///
/// event: claim
/// data: {...}
/// ```
///
/// The stream closes (no explicit `turn_closed` event needed) when
/// the sender side is dropped  `turn_end` removes the channel,
/// the receiver yields `None`, the stream completes. Python
/// loop driver detects close via the EventSource's `onerror` /
/// async-iterator end and proceeds to the gate stack with the
/// drained list.
///
/// KeepAlive comments fire every 15 s so an idle stream (codex is
/// thinking, no chips yet) doesn't get reaped by intermediate
/// proxies. None today (only the docker compose network is in
/// the path), but future-proofs against running this through a
/// reverse proxy or k8s ingress.
pub async fn stream_claims(
    State(state): State<AppState>,
    Path(snapshot_id): Path<String>,
) -> Response {
    // Take the receiver (single-consumer enforcement). Missing
    // entry => snapshot was never opened (or already ended).
    // Empty Option inside => somebody already drained this turn.
    let rx = match state.claim_receivers.get(&snapshot_id) {
        Some(slot) => match slot.lock().take() {
            Some(rx) => rx,
            None => {
                return (
                    StatusCode::CONFLICT,
                    format!(
                        "claims drain for snapshot {snapshot_id} already opened by another consumer"
                    ),
                )
                    .into_response();
            }
        },
        None => {
            return (
                StatusCode::NOT_FOUND,
                format!("snapshot {snapshot_id} not open; call /turn/begin first"),
            )
                .into_response();
        }
    };

    // Map each `serde_json::Value` from the channel into an SSE
    // `event: claim` frame. The unwrap on `serde_json::to_string`
    // is safe: the values were `serde_json::to_value`-built by
    // the MCP tool from valid serde structs.
    let stream = UnboundedReceiverStream::new(rx).map(|claim_value| {
        let data = serde_json::to_string(&claim_value).unwrap_or_else(|_| "null".to_string());
        Ok::<Event, Infallible>(Event::default().event("claim").data(data))
    });

    Sse::new(stream)
        .keep_alive(KeepAlive::new().interval(std::time::Duration::from_secs(15)))
        .into_response()
}

pub async fn wallet_profile_route(State(state): State<AppState>, req: Request) -> Response {
    let format = match WireFormat::from_headers(req.headers()) {
        Ok(f) => f,
        Err(e) => return e,
    };
    let body = match read_body(req).await {
        Ok(b) => b,
        Err(e) => return e,
    };
    let req_msg: proto::WalletProfileRequest = match decode_request(format, &body) {
        Ok(m) => m,
        Err(e) => return e,
    };

    let snapshot = match state.snapshot_cache.get(&req_msg.snapshot_id) {
        Some(s) => s,
        None => return snapshot_gone(&req_msg.snapshot_id, format),
    };

    let input_proto = match req_msg.input.into_option() {
        Some(i) => i,
        None => {
            return bridge_error_response(
                BridgeError::MissingField("WalletProfileRequest.input"),
                format,
            );
        }
    };

    let internal_input = match proto_bridge::proto_to_internal_wallet_input(input_proto) {
        Ok(i) => i,
        Err(e) => return bridge_error_response(e, format),
    };

    let internal_out: PrimitiveOutput<wallet_profile::WalletProfileOutput> =
        match wallet_profile::compute_with_snapshot(&state, &snapshot, internal_input).await {
            Ok(o) => o,
            Err(e) => return primitive_error_response(e, format),
        };

    let envelope = match proto_bridge::build_envelope(internal_out) {
        Ok(e) => e,
        Err(e) => return bridge_error_response(e, format),
    };

    encode_response(format, &envelope, StatusCode::OK)
}

pub async fn community_summary_route(State(state): State<AppState>, req: Request) -> Response {
    let format = match WireFormat::from_headers(req.headers()) {
        Ok(f) => f,
        Err(e) => return e,
    };
    let body = match read_body(req).await {
        Ok(b) => b,
        Err(e) => return e,
    };
    let req_msg: proto::CommunitySummaryRequest = match decode_request(format, &body) {
        Ok(m) => m,
        Err(e) => return e,
    };

    let snapshot = match state.snapshot_cache.get(&req_msg.snapshot_id) {
        Some(s) => s,
        None => return snapshot_gone(&req_msg.snapshot_id, format),
    };

    let input_proto = match req_msg.input.into_option() {
        Some(i) => i,
        None => {
            return bridge_error_response(
                BridgeError::MissingField("CommunitySummaryRequest.input"),
                format,
            );
        }
    };

    let internal_input = match proto_bridge::proto_to_internal_community_input(input_proto) {
        Ok(i) => i,
        Err(e) => return bridge_error_response(e, format),
    };

    let internal_out: PrimitiveOutput<community_summary::CommunitySummaryOutput> =
        match community_summary::compute_with_snapshot(&state, &snapshot, internal_input).await {
            Ok(o) => o,
            Err(e) => return primitive_error_response(e, format),
        };

    let envelope = match proto_bridge::build_envelope(internal_out) {
        Ok(e) => e,
        Err(e) => return bridge_error_response(e, format),
    };

    encode_response(format, &envelope, StatusCode::OK)
}

/// `POST /primitive/get_token_info`. Resolves a mint pubkey to its
/// on-chain `name / symbol / uri` via `crate::metadata::fetch`. The
/// request envelope's `snapshot_id` is ignored; resolution is stateless
/// per call.
///
/// Lazy ClickHouse-backed cache: the first resolution of a given mint
/// fires `getAccountInfo` calls (Metaplex PDA, then Token-2022
/// fallback) and writes the result to `multichain.token_metadata`.
/// Subsequent calls within `METADATA_CACHE_TTL_SLOTS` of the chain tip
/// (default 9000 slots, ~1 hour) are served from the cache without
/// touching RPC. Stale rows trigger a re-fetch on the next read; the
/// TTL bounds staleness during the gap before issue #48 (CDC
/// instruction decoding) lands and keeps the cache fresh by ingest-
/// time writes instead.
///
/// No allowlist. The route lives on the internal-only HTTP listener
/// (`internal_router` in `api::mod`), so the only intended caller is
/// the Python agent-service container on the docker compose network;
/// browser-side abuse against an arbitrary mint pubkey is not
/// reachable. The earlier live-window allowlist gate became
/// counterproductive once the cache returned: blocking reads for
/// mints not currently in the 60-second window contradicts the
/// cache's purpose ("we have the metadata but won't tell you").
///
/// Untrusted text (the resolved name/symbol/uri) is NOT sanitized
/// here. Per the project's tool-output convention, the agent-service
/// `get_token_info` tool wraps the returned strings in
/// `<external_data primitive="get_token_info">...</external_data>`
/// before they reach the model, gated additionally by the
/// `external_text_input_enabled` channel switch.
pub async fn get_token_info_route(State(state): State<AppState>, req: Request) -> Response {
    let format = match WireFormat::from_headers(req.headers()) {
        Ok(f) => f,
        Err(e) => return e,
    };
    let body = match read_body(req).await {
        Ok(b) => b,
        Err(e) => return e,
    };
    let req_msg: proto::GetTokenInfoRequest = match decode_request(format, &body) {
        Ok(m) => m,
        Err(e) => return e,
    };

    let input = match req_msg.input.into_option() {
        Some(i) => i,
        None => {
            return bridge_error_response(
                BridgeError::MissingField("GetTokenInfoRequest.input"),
                format,
            );
        }
    };

    // Compute lives in `crate::primitives::get_token_info` so the new
    // MCP tool wrapper (`crate::mcp::McaeMcp::get_token_info`) calls
    // the same path. The HTTP route owns the proto-bridge mapping;
    // compute returns a serde struct shape that we map to the
    // wire-level `proto::GetTokenInfoOutput` here.
    use crate::primitives::get_token_info::{GetTokenInfoError, compute};

    let resp = match compute(&state, &input.mint).await {
        Ok(out) => proto::GetTokenInfoOutput {
            mint: out.mint,
            name: out.name,
            symbol: out.symbol,
            uri: out.uri,
            update_authority: out.update_authority,
            source_program: out.source_program,
            ..Default::default()
        },
        Err(GetTokenInfoError::InvalidMint(msg)) => {
            return error_body(StatusCode::BAD_REQUEST, "invalid_input", &msg, format);
        }
        Err(e @ GetTokenInfoError::RpcDisabled) => {
            return error_body(
                StatusCode::SERVICE_UNAVAILABLE,
                "rpc_disabled",
                &e.to_string(),
                format,
            );
        }
        Err(GetTokenInfoError::RpcError(msg)) => {
            return error_body(StatusCode::BAD_GATEWAY, "rpc_error", &msg, format);
        }
    };
    encode_response(format, &resp, StatusCode::OK)
}

