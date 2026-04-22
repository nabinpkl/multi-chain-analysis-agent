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

    let (sm_stream_start, sm_latest_block, sm_edge_pairs, sm_wallets, sm_ring) = {
        let sm = state.state_machine.read();
        (
            sm.stream_start_ts(),
            sm.latest_block_time(),
            sm.edge_agg_len(),
            sm.wallet_agg_len(),
            sm.ring_len(),
        )
    };

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
            },
            "state_machine": {
                "stream_start_ts": sm_stream_start,
                "latest_block_time": sm_latest_block,
                "edge_pairs": sm_edge_pairs,
                "wallets": sm_wallets,
                "ring_entries": sm_ring,
            }
        })),
    )
}
