use std::convert::Infallible;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::{Query, State};
use axum::response::Sse;
use axum::response::sse::{Event, KeepAlive};
use futures_util::stream::{Stream, StreamExt};
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;

use crate::api::graph::{OverviewParams, parse_window};
use crate::domain::OverviewResponse;
use crate::state::AppState;

/// `GET /graph/overview/stream?window=15m|1h|6h|24h`  SSE.
/// Sends an initial `snapshot` event, then a fresh `snapshot` each tick
/// the state machine advances. Each connection owns its own window 
/// the server recomputes per-subscriber on each tick so viewers picking
/// different windows get different data. Slow-subscriber backpressure
/// is surfaced as a `resync` event; client is expected to refetch
/// `/graph/overview` for the same window and re-open the stream.
pub async fn stream(
    State(state): State<AppState>,
    Query(params): Query<OverviewParams>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let window_secs = parse_window(params.window.as_deref(), state.window_secs);

    let initial = {
        let now = now_secs();
        let mut resp = state.state_machine.read().snapshot_window(now, window_secs);
        crate::layout::stamp_positions(&mut resp.nodes, &state.positions.read());
        Arc::new(resp)
    };

    let init_stream = futures_util::stream::once(async move {
        Ok(snapshot_event(&initial))
    });

    let sm = state.state_machine.clone();
    let positions = state.positions.clone();
    let rx = state.tick_tx.subscribe();
    let updates = BroadcastStream::new(rx).map(move |res| -> Result<Event, Infallible> {
        match res {
            Ok(()) => {
                let now = now_secs();
                let mut resp = sm.read().snapshot_window(now, window_secs);
                crate::layout::stamp_positions(&mut resp.nodes, &positions.read());
                let snap = Arc::new(resp);
                Ok(snapshot_event(&snap))
            }
            Err(BroadcastStreamRecvError::Lagged(_)) => Ok(Event::default()
                .event("resync")
                .data("broadcast lag  client should refetch /graph/overview")),
        }
    });

    Sse::new(init_stream.chain(updates))
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

fn now_secs() -> u32 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as u32)
        .unwrap_or(0)
}

fn snapshot_event(snap: &Arc<OverviewResponse>) -> Event {
    match Event::default().event("snapshot").json_data(snap.as_ref()) {
        Ok(ev) => ev,
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize failed: {e}")),
    }
}
