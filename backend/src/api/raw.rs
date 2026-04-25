use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::State;
use axum::response::Sse;
use axum::response::sse::{Event, KeepAlive};
use futures_util::stream::{Stream, StreamExt};
use serde::Serialize;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;

use crate::domain::{Edge, LAMPORTS_PER_SOL};
use crate::state::AppState;

/// `GET /graph/raw/stream`  fire-hose of every ingested edge. No
/// snapshot, no window, no backend layout. One SSE `edge` event per
/// transaction. Clients decide how to render; hubs emerge naturally
/// because popular wallets appear in many edges.
///
/// On broadcast lag (slow subscriber), emits a `lag` event and the
/// client is expected to accept the gap  there is no snapshot to
/// resync to. Missing edges stay missing.
pub async fn stream(
    State(state): State<AppState>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let rx = state.raw_tx.subscribe();
    let updates = BroadcastStream::new(rx).map(|res| -> Result<Event, Infallible> {
        match res {
            Ok(edge) => Ok(edge_event(&edge)),
            Err(BroadcastStreamRecvError::Lagged(n)) => Ok(Event::default()
                .event("lag")
                .data(format!("missed {n} edges (broadcast buffer overrun)"))),
        }
    });

    Sse::new(updates).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

#[derive(Serialize)]
struct EdgeWire<'a> {
    signature: &'a str,
    block_time: u32,
    from: &'a str,
    to: &'a str,
    volume_sol: f64,
}

fn edge_event(edge: &Arc<Edge>) -> Event {
    let wire = EdgeWire {
        signature: &edge.signature,
        block_time: edge.block_time,
        from: &edge.from_wallet,
        to: &edge.to_wallet,
        volume_sol: edge.amount as f64 / LAMPORTS_PER_SOL,
    };
    match Event::default().event("edge").json_data(&wire) {
        Ok(ev) => ev,
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize failed: {e}")),
    }
}
