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

use axum::body::{Body, Bytes};
use axum::extract::{FromRequest, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};

use buffa::Message;
use serde_json::json;
use solana_pubkey::Pubkey;

use crate::metadata;
use crate::primitives::{
    PrimitiveError, PrimitiveOutput, community_summary, wallet_profile,
};
use crate::snapshot::{TurnSnapshot, current_time_ms};
use crate::graph::window::window_index;
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

/// `POST /turn/begin`. Materializes a `TurnSnapshot` against the live
/// 60s window, stashes it in the cache under a fresh `snapshot_id`
/// (ulid for sortability), returns the lease descriptor.
///
/// Begin requests carry no body, so we don't sniff Content-Type for
/// the request side. Response wire format defaults to JSON unless
/// `Accept: application/x-protobuf` is set, matching how curl users
/// expect to see the lease.
pub async fn turn_begin(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let format = match headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.split(',').next().unwrap_or("").trim())
    {
        Some(CT_PROTOBUF) => WireFormat::Protobuf,
        _ => WireFormat::Json,
    };

    let live_window_idx = window_index(60).unwrap_or(1);
    let analytics = state.analytics.snapshots[live_window_idx].borrow().clone();

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

    let resp = proto::SnapshotBeginResponse {
        snapshot_id,
        expires_at_ms,
        window_secs: 60,
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
    StatusCode::NO_CONTENT.into_response()
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
    let mint_b58 = input.mint.trim().to_string();
    if mint_b58.is_empty() {
        return error_body(
            StatusCode::BAD_REQUEST,
            "invalid_input",
            "mint pubkey is empty",
            format,
        );
    }
    let mint_pk = match parse_pubkey(&mint_b58) {
        Ok(pk) => pk,
        Err(msg) => {
            return error_body(StatusCode::BAD_REQUEST, "invalid_input", &msg, format);
        }
    };

    let rpc = match state.rpc.clone() {
        Some(r) => r,
        None => {
            return error_body(
                StatusCode::SERVICE_UNAVAILABLE,
                "rpc_disabled",
                "SOLANA_RPC_URL is not configured; get_token_info needs RPC access",
                format,
            );
        }
    };

    let cache_ctx = metadata::fetch::CacheCtx {
        clickhouse: &state.clickhouse,
        // Tip-unknown sentinel = 0; cache::read_cached treats every
        // row as stale until the first `getSlot` round-trip lands.
        current_slot: state.tip.current().unwrap_or(0),
        ttl_slots: state.metadata_cache_ttl_slots,
    };
    let metadata_opt = match metadata::fetch::fetch_token_metadata(&rpc, &mint_pk, &cache_ctx).await
    {
        Ok(o) => o,
        Err(e) => {
            return error_body(
                StatusCode::BAD_GATEWAY,
                "rpc_error",
                &format!("getAccountInfo failed: {e}"),
                format,
            );
        }
    };

    let resp = match metadata_opt {
        Some(meta) => proto::GetTokenInfoOutput {
            mint: mint_b58,
            name: Some(meta.name),
            symbol: Some(meta.symbol),
            uri: Some(meta.uri),
            update_authority: Some(meta.update_authority),
            source_program: meta.program.to_string(),
            ..Default::default()
        },
        // Mint exists on chain but has no metadata via either path.
        None => proto::GetTokenInfoOutput {
            mint: mint_b58,
            source_program: String::new(),
            ..Default::default()
        },
    };
    encode_response(format, &resp, StatusCode::OK)
}

fn parse_pubkey(s: &str) -> Result<Pubkey, String> {
    let mut bytes = [0u8; 32];
    let written = bs58::decode(s)
        .onto(&mut bytes[..])
        .map_err(|e| format!("invalid base58: {e}"))?;
    if written != 32 {
        return Err(format!(
            "invalid pubkey length: expected 32 bytes, got {written}"
        ));
    }
    Ok(Pubkey::new_from_array(bytes))
}

