/// `GET /graph/stream?window={60|300|900|1800|3600}`
///
/// SSE endpoint implementing differential rendering. Each rolling window
/// has its own broadcast channel; subscribers see only events relevant
/// to their window. Defaults to 3600s when `window` is omitted.
///
/// On every connect:
/// 1. Validate `window` param.
/// 2. Subscribe to that window's broadcast channel BEFORE acquiring the
///    read lock so deltas between snapshot and live tail aren't dropped.
/// 3. Snapshot bootstrap events restricted to the chosen window.
/// 4. Snapshot `live_seq_at_release = graph.current_seq()`.
/// 5. Emit bootstrap events without `id:`, then `CaughtUp` with `id`,
///    then live tail with `id`.
///
/// `?skip_bootstrap=1` omits the cold-start phase.
use std::collections::HashMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response, Sse};
use axum::response::sse::{Event, KeepAlive};
use futures_util::stream::StreamExt;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;

use crate::graph::bootstrap::bootstrap_events;
use crate::graph::delta::GraphDelta;
use crate::graph::window::parse_window_param;
use crate::state::AppState;

pub async fn stream(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    let window_idx = match parse_window_param(params.get("window").map(|s| s.as_str())) {
        Ok(w) => w,
        Err(msg) => return (StatusCode::BAD_REQUEST, msg).into_response(),
    };
    let skip_bootstrap = params.get("skip_bootstrap").map(|v| v == "1").unwrap_or(false);

    let rx = state.deltas.sender(window_idx).subscribe();

    let (bootstrap, live_seq_at_release) = {
        let graph = state.graph.read();
        let live_seq = graph.current_seq();
        let events = if skip_bootstrap {
            vec![]
        } else {
            bootstrap_events(&graph, window_idx)
        };
        (events, live_seq)
    };

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
            true,
        );
        Ok::<Event, Infallible>(ev)
    });

    let live_stream = BroadcastStream::new(rx).flat_map(|res| {
        let items: Vec<Result<Event, Infallible>> = match res {
            Ok(batch) => batch
                .iter()
                .map(|delta| Ok(delta_to_sse_event(delta, true)))
                .collect(),
            Err(BroadcastStreamRecvError::Lagged(n)) => {
                tracing::warn!(missed = n, "graph/stream: subscriber lagged, missed deltas");
                vec![]
            }
        };
        futures_util::stream::iter(items)
    });

    let combined = bootstrap_stream
        .chain(caught_up_event)
        .chain(live_stream);

    Sse::new(combined)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
        .into_response()
}

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

