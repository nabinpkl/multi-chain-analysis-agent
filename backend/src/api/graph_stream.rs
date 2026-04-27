/// `GET /graph/stream`
///
/// SSE endpoint implementing the slice 2 differential rendering protocol.
///
/// On every connect:
/// 1. Acquire read lock on GraphState.
/// 2. Snapshot `live_seq_at_release = graph.current_seq()`.
/// 3. Subscribe to `delta_tx` broadcast BEFORE releasing the lock so no
///    deltas are missed between bootstrap and live tail.
/// 4. Iterate bootstrap events under read lock; emit each WITHOUT `id:`.
///    (Skipped when `?skip_bootstrap=1` is set.)
/// 5. Emit `CaughtUp { seq: live_seq_at_release }` WITH `id: live_seq_at_release`.
/// 6. Release lock.
/// 7. Forward live broadcast deltas with `id: <seq>`.
///
/// `?skip_bootstrap=1`: omit the NodeAdded/EdgeAdded/ComponentAssigned
/// bootstrap phase and immediately emit CaughtUp, then tail live deltas.
/// Default (no param) keeps full cold-start behavior unchanged.
///
/// No ring buffer, no `Last-Event-ID` resume. Always cold-start.
use std::collections::HashMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::response::Sse;
use axum::response::sse::{Event, KeepAlive};
use futures_util::stream::{Stream, StreamExt};
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;

use crate::graph::bootstrap::bootstrap_events;
use crate::graph::delta::GraphDelta;
use crate::state::AppState;

pub async fn stream(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let skip_bootstrap = params.get("skip_bootstrap").map(|v| v == "1").unwrap_or(false);

    // Subscribe to broadcast BEFORE acquiring read lock so we don't miss
    // any deltas between the snapshot and the live tail.
    let rx = state.delta_tx.subscribe();

    // Snapshot bootstrap events + the seq at snapshot time.
    let (bootstrap, live_seq_at_release) = {
        let graph = state.graph.read();
        let live_seq = graph.current_seq();
        let events = if skip_bootstrap {
            vec![]
        } else {
            bootstrap_events(&graph)
        };
        (events, live_seq)
    };

    // Build the stream: bootstrap events first, then CaughtUp, then live.
    let bootstrap_stream = futures_util::stream::iter(
        bootstrap
            .into_iter()
            .map(|delta| Ok::<Event, Infallible>(delta_to_sse_event(&delta, false))),
    );

    let caught_up_event = futures_util::stream::once(async move {
        let ev = delta_to_sse_event(
            &GraphDelta::CaughtUp {
                seq: live_seq_at_release,
            },
            true, // include id: field
        );
        Ok::<Event, Infallible>(ev)
    });

    let live_stream = BroadcastStream::new(rx).flat_map(|res| {
        let items: Vec<Result<Event, Infallible>> = match res {
            Ok(batch) => batch
                .iter()
                .map(|delta| {
                    let ev = delta_to_sse_event(delta, true);
                    Ok(ev)
                })
                .collect(),
            Err(BroadcastStreamRecvError::Lagged(n)) => {
                // Slow subscriber missed deltas. Log and continue  the
                // client will reconnect and get a fresh cold-start.
                tracing::warn!(missed = n, "graph/stream: subscriber lagged, missed deltas");
                vec![]
            }
        };
        futures_util::stream::iter(items)
    });

    let combined = bootstrap_stream
        .chain(caught_up_event)
        .chain(live_stream);

    Sse::new(combined).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

/// Serialize a GraphDelta as an SSE Event. Bootstrap events (`with_id=false`)
/// omit the `id:` field so browsers don't use them as resume points.
/// Live events (`with_id=true`) include `id: <seq>`.
fn delta_to_sse_event(delta: &GraphDelta, with_id: bool) -> Event {
    let event_type = match delta {
        GraphDelta::NodeAdded { .. } => "NodeAdded",
        GraphDelta::EdgeAdded { .. } => "EdgeAdded",
        GraphDelta::ComponentAssigned { .. } => "ComponentAssigned",
        GraphDelta::EdgeExpired { .. } => "EdgeExpired",
        GraphDelta::NodeExpired { .. } => "NodeExpired",
        GraphDelta::PositionsBatch { .. } => "PositionsBatch",
        GraphDelta::CaughtUp { .. } => "CaughtUp",
    };

    let ev = Event::default().event(event_type);

    let ev = match serde_json::to_string(delta) {
        Ok(json) => ev.data(json),
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize failed: {e}")),
    };

    if with_id {
        ev.id(delta.seq().to_string())
    } else {
        ev
    }
}
