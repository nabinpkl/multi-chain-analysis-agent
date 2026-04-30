//! Agent diagnostics endpoint. Surfaces:
//! - Configured provider + primary model.
//! - Active stub registry entries (name, reason, ship that promotes,
//!   hits, registered_at).
//! - Registered primitive names.
//!
//! The frontend's stub-banner polls this every 10s. Visible-stubs is
//! a foundation per the ship-1 plan: stubs must never be silent.

use axum::Json;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use serde::Serialize;
use ts_rs::TS;

use crate::agent::AgentClient;
use crate::agent::stubs::StubInfoWire;
use crate::state::AppState;

#[derive(Serialize, TS, Debug, Clone)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AgentDiagnostics {
    pub provider: String,
    pub primary_model: String,
    pub stubs: Vec<StubInfoWire>,
    pub registered_primitives: Vec<String>,
}

pub async fn diagnostics(State(state): State<AppState>) -> Response {
    let (provider, primary_model) = match &state.agent_client {
        Some(AgentClient::OpenRouter {
            primary_model,
            ..
        }) => ("openrouter".to_string(), primary_model.clone()),
        None => ("disabled".to_string(), "(none)".to_string()),
    };
    let stubs = state.agent_stubs.snapshot();
    let registered_primitives = state
        .agent_registry
        .names()
        .into_iter()
        .map(|s| s.to_string())
        .collect();
    let body = AgentDiagnostics {
        provider,
        primary_model,
        stubs,
        registered_primitives,
    };
    Json(body).into_response()
}
