/// `GET /graph/stream?window={10|60|300|900|1800|3600}`
///
/// SSE endpoint implementing differential rendering. Each rolling window
/// has its own broadcast channel; subscribers see only events relevant
/// to their window. Defaults to 3600s when `window` is omitted.
///
/// On every connect:
/// 1. Validate `window` param.
/// 2. Subscribe to that window's edge broadcast channel AND analytics
///    broadcast channel BEFORE acquiring the read lock so deltas between
///    snapshot and live tail aren't dropped.
/// 3. Snapshot bootstrap edge events restricted to the chosen window.
/// 4. Snapshot `live_seq_at_release = graph.current_seq()`.
/// 5. Read the latest `AnalyticsSnapshot` from the watch channel and
///    fold every label into a single bootstrap `AnalyticsBatch`.
/// 6. Emit edge bootstrap events without `id:`, then the analytics
///    bootstrap batch (no `id:`), then `CaughtUp` with `id`, then
///    multiplexed live tail (edges + analytics) with `id`.
///
/// `?skip_bootstrap=1` omits the cold-start phase (both edge and
/// analytics).
use std::collections::HashMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive};
use axum::response::{IntoResponse, Response, Sse};
use futures_util::stream::StreamExt;
use tokio_stream::wrappers::BroadcastStream;
use tokio_stream::wrappers::errors::BroadcastStreamRecvError;

use crate::analytics::AnalyticsBatch;
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

    // Subscribe to both channels BEFORE reading any state so nothing
    // produced between snapshot and live tail is missed.
    let edge_rx = state.deltas.sender(window_idx).subscribe();
    let analytics_rx = state.analytics.sender(window_idx).subscribe();

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

    // Read the latest analytics snapshot off the watch channel and
    // fold it into a single bootstrap `AnalyticsBatch`. Empty when
    // no tick has run yet or `skip_bootstrap` is set.
    let analytics_bootstrap = if skip_bootstrap {
        None
    } else {
        let snap = state.analytics.snapshots[window_idx].borrow().clone();
        if snap.labels.is_empty() && snap.roles.is_empty() {
            None
        } else {
            let community_changes: Vec<(u32, u32)> =
                snap.labels.iter().map(|(&n, &c)| (n, c)).collect();
            let role_changes: Vec<(u32, _)> =
                snap.roles.iter().map(|(&n, &r)| (n, r)).collect();
            Some(AnalyticsBatch {
                epoch: snap.epoch,
                community_changes,
                community_removals: Vec::new(),
                role_changes,
                role_removals: Vec::new(),
            })
        }
    };

    let edge_bootstrap_stream = futures_util::stream::iter(
        bootstrap
            .into_iter()
            .map(|delta| Ok::<Event, Infallible>(delta_to_sse_event(&delta, false))),
    );

    let analytics_bootstrap_stream =
        futures_util::stream::iter(analytics_bootstrap.into_iter().map(|batch| {
            Ok::<Event, Infallible>(analytics_to_sse_event(&batch, false))
        }));

    let caught_up_event = futures_util::stream::once(async move {
        let ev = delta_to_sse_event(
            &GraphDelta::CaughtUp {
                seq: live_seq_at_release,
            },
            true,
        );
        Ok::<Event, Infallible>(ev)
    });

    let edge_live = BroadcastStream::new(edge_rx).flat_map(|res| {
        let items: Vec<Result<Event, Infallible>> = match res {
            Ok(batch) => batch
                .iter()
                .map(|delta| Ok(delta_to_sse_event(delta, true)))
                .collect(),
            Err(BroadcastStreamRecvError::Lagged(n)) => {
                tracing::warn!(missed = n, "graph/stream: edge subscriber lagged, missed deltas");
                vec![]
            }
        };
        futures_util::stream::iter(items)
    });

    let analytics_live = BroadcastStream::new(analytics_rx).flat_map(|res| {
        let items: Vec<Result<Event, Infallible>> = match res {
            Ok(batch) => vec![Ok(analytics_to_sse_event(&batch, true))],
            Err(BroadcastStreamRecvError::Lagged(n)) => {
                tracing::warn!(
                    missed = n,
                    "graph/stream: analytics subscriber lagged, missed batches"
                );
                vec![]
            }
        };
        futures_util::stream::iter(items)
    });

    // Multiplex live tail. `select` interleaves edge + analytics
    // events as they arrive; ordering across the two streams is
    // not required because analytics carries its own monotonic
    // `epoch` for de-dupe.
    let live_stream = futures_util::stream::select(edge_live, analytics_live);

    let combined = edge_bootstrap_stream
        .chain(analytics_bootstrap_stream)
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
        GraphDelta::EdgeExpired { .. } => "EdgeExpired",
        GraphDelta::NodeExpired { .. } => "NodeExpired",
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

fn analytics_to_sse_event(batch: &AnalyticsBatch, with_id: bool) -> Event {
    let ev = Event::default().event("AnalyticsBatch");
    let ev = match serde_json::to_string(batch) {
        Ok(json) => ev.data(json),
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize failed: {e}")),
    };
    if with_id {
        // Use a namespaced id so the SSE Last-Event-ID auto-resume
        // can't collide with edge `seq` ids.
        ev.id(format!("analytics:{}", batch.epoch))
    } else {
        ev
    }
}

