//! Agent diagnostics endpoint. Surfaces only operator-safe fields
//! (stub registry + registered primitive names) to the frontend.
//!
//! Ship 2.6 scrubbed model + provider identifiers from this wire:
//! constitution Rule 4 says "the agent's identity is the analyst,
//! not the model behind it", and the stub-banner used to contradict
//! that by showing `openrouter/nemotron-...` next to a header chip.
//! Provider + primary model + policy model identifiers stay in
//! server-side `tracing` logs (`policy gate online`, `agent client
//! constructed`, etc.) where operators read them; the frontend
//! never sees them. If a richer admin view is needed later, gate it
//! behind a header check rather than re-broadening this endpoint.
//!
//! Whether the agent is `enabled: true` is the only model-adjacent
//! signal that ships: a binary so the frontend can render "agent
//! disabled" copy when `AGENT_API_KEY` is unset, without naming any
//! provider.

use axum::Json;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde::Serialize;
use ts_rs::TS;

use crate::agent::stubs::StubInfoWire;
use crate::state::AppState;

#[derive(Serialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentDiagnostics {
    /// True when the agent client constructed (API key present); false
    /// when boot found no key and agent endpoints will 503. Frontend
    /// uses this for rendering an "agent unavailable" state without
    /// naming any model or provider.
    pub enabled: bool,
    pub stubs: Vec<StubInfoWire>,
    pub registered_primitives: Vec<String>,
}

pub async fn diagnostics(State(state): State<AppState>) -> Response {
    let enabled = state.agent_client.is_some();
    let stubs = state.agent_stubs.snapshot();
    let registered_primitives = state
        .agent_registry
        .names()
        .into_iter()
        .map(|s| s.to_string())
        .collect();
    let body = AgentDiagnostics {
        enabled,
        stubs,
        registered_primitives,
    };
    Json(body).into_response()
}
