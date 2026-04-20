use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use serde_json::{Value, json};

use crate::state::AppState;

const COMPONENT: &str = "solana_ingester";
const SLOT_SECONDS: f64 = 0.4;

pub async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

pub async fn ready(State(state): State<AppState>) -> (StatusCode, Json<Value>) {
    if let Err(e) = state.clickhouse.query("SELECT 1").execute().await {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "status": "degraded", "clickhouse": e.to_string() })),
        );
    }

    let last_slot = match state.store.get_last_slot(COMPONENT).await {
        Ok(slot) => slot,
        Err(e) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({ "status": "degraded", "ingester": e.to_string() })),
            );
        }
    };

    let tip_slot = state.tip.current();
    let lag_slots = match (tip_slot, last_slot) {
        (Some(tip), Some(last)) => Some(tip.saturating_sub(last)),
        _ => None,
    };
    let lag_seconds = lag_slots.map(|l| (l as f64 * SLOT_SECONDS).round() as u64);

    (
        StatusCode::OK,
        Json(json!({
            "status": "ready",
            "clickhouse": "ok",
            "ingester": {
                "last_slot": last_slot,
                "tip_slot": tip_slot,
                "lag_slots": lag_slots,
                "lag_seconds_approx": lag_seconds
            }
        })),
    )
}
