use std::time::{SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::{Query, State};
use serde::Deserialize;

use crate::domain::OverviewResponse;
use crate::state::AppState;

#[derive(Debug, Deserialize)]
pub struct OverviewParams {
    pub window: Option<String>,
}

/// Parse a window label into seconds. Unknown / missing → the state
/// machine's configured max (24h). The slow-path scan inside the state
/// machine clamps to `self.window_secs` anyway so callers can't request
/// more than we maintain.
pub fn parse_window(label: Option<&str>, max: u32) -> u32 {
    match label {
        Some("15m") => 15 * 60,
        Some("1h") => 60 * 60,
        Some("6h") => 6 * 60 * 60,
        Some("24h") => 24 * 60 * 60,
        _ => max,
    }
    .min(max)
}

/// `GET /graph/overview?window=15m|1h|6h|24h` — reads the live projection
/// from the in-memory state machine, scoped to the requested window.
pub async fn overview(
    State(state): State<AppState>,
    Query(params): Query<OverviewParams>,
) -> Json<OverviewResponse> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as u32)
        .unwrap_or(0);
    let window_secs = parse_window(params.window.as_deref(), state.window_secs);
    let snapshot = state.state_machine.read().snapshot_window(now, window_secs);
    Json(snapshot)
}
