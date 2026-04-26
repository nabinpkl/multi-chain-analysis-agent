use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::State;
use axum::response::Sse;
use axum::response::sse::{Event, KeepAlive};
use futures_util::stream::{Stream, StreamExt};
use serde::Serialize;
use ts_rs::TS;
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

/// Token-issuance / destruction direction on the wire. Only present for
/// SPL edges that originate from or terminate at a mint authority.
#[derive(Serialize, TS)]
#[serde(rename_all = "lowercase")]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
enum EdgeKind {
    Mint,
    Burn,
}

#[derive(Serialize, TS)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
struct EdgeWire<'a> {
    signature: &'a str,
    block_time: u32,
    from: &'a str,
    to: &'a str,
    /// SOL volume only. For SPL transfers (`mint` present), this is 0
    /// because the amount is in unknown token base units; the frontend
    /// uses edge presence + degree only for SPL.
    volume_sol: f64,
    /// SPL/Token-2022 mint pubkey if this edge represents a token
    /// transfer. Absent for native SOL.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[ts(optional)]
    mint: Option<&'a str>,
    /// Token issuance or destruction direction. Absent for regular transfers.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[ts(optional)]
    kind: Option<EdgeKind>,
}

fn edge_event(edge: &Arc<Edge>) -> Event {
    let is_sol = edge.mint.is_empty();
    let wire = EdgeWire {
        signature: &edge.signature,
        block_time: edge.block_time,
        from: &edge.from_wallet,
        to: &edge.to_wallet,
        volume_sol: if is_sol {
            edge.amount as f64 / LAMPORTS_PER_SOL
        } else {
            0.0
        },
        mint: if is_sol { None } else { Some(&edge.mint) },
        kind: match edge.kind.as_str() {
            "mint" => Some(EdgeKind::Mint),
            "burn" => Some(EdgeKind::Burn),
            _ => None,
        },
    };
    match Event::default().event("edge").json_data(&wire) {
        Ok(ev) => ev,
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize failed: {e}")),
    }
}
