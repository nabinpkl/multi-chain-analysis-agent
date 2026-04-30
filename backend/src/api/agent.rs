//! Agent HTTP routes.
//!
//! `POST /agent/ask`: client sends an `AgentRequest`; server generates
//! a session_id, stores the request in the in-memory session map,
//! returns the id.
//!
//! `GET /agent/stream/:session_id`: SSE channel. Drives the per-session
//! loop (see `agent/loop.rs`); emits `Claim`, `Progress`, and `Done`
//! events as the loop produces them.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive};
use axum::response::{IntoResponse, Response, Sse};
use futures_util::StreamExt;
use parking_lot::Mutex;
use rustc_hash::FxHashMap;
use tokio_stream::wrappers::ReceiverStream;
use tracing::info;

use crate::agent::AgentThread;
use crate::agent::SseFrame;
use crate::agent::loop_driver;
use crate::agent::types::{AgentDone, AgentRequest, AgentSessionStarted};
use crate::state::AppState;

/// Pending turn ready to be picked up by SSE GET. Carries the
/// resolved thread_id (either echoed from the AgentRequest or freshly
/// minted) and the turn number (0 on first turn of a thread, then
/// increments per follow-up).
#[derive(Clone)]
pub struct PendingTurn {
    pub request: AgentRequest,
    pub thread_id: String,
    pub turn: u32,
}

/// Pending turns keyed by session_id (per-turn handle). Inserted by
/// POST, removed by the matching SSE GET.
#[derive(Default, Clone)]
pub struct AgentSessions {
    inner: Arc<Mutex<FxHashMap<String, PendingTurn>>>,
}

impl AgentSessions {
    pub fn new() -> Self {
        Self::default()
    }

    fn insert(&self, id: String, turn: PendingTurn) {
        let mut g = self.inner.lock();
        g.insert(id, turn);
    }

    fn take(&self, id: &str) -> Option<PendingTurn> {
        let mut g = self.inner.lock();
        g.remove(id)
    }
}

pub async fn ask(
    State(state): State<AppState>,
    Json(req): Json<AgentRequest>,
) -> Response {
    if state.agent_client.is_none() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            "agent disabled: AGENT_API_KEY not set",
        )
            .into_response();
    }
    let session_id = generate_session_id();

    // Resolve thread_id: echo what the client sent if it points to an
    // existing thread; otherwise mint a fresh one. We don't trust
    // unknown ids (a client sending a fabricated id gets a fresh
    // thread, not a foreign thread). Turn count is 0 for new threads,
    // current turn_count for follow-ups.
    let (thread_id, turn) = {
        let mut threads = state.agent_threads.lock();
        if let Some(provided) = req.thread_id.as_ref() {
            if let Some(existing) = threads.get(provided) {
                (provided.clone(), existing.turn_count)
            } else {
                let id = generate_session_id();
                threads.insert(
                    id.clone(),
                    AgentThread::new(id.clone(), now_ms_utc()),
                );
                (id, 0)
            }
        } else {
            let id = generate_session_id();
            threads.insert(
                id.clone(),
                AgentThread::new(id.clone(), now_ms_utc()),
            );
            (id, 0)
        }
    };

    info!(
        session_id = %session_id,
        thread_id = %thread_id,
        turn,
        q = %req.user_question,
        "agent ask received"
    );

    state.agent_sessions.insert(
        session_id.clone(),
        PendingTurn {
            request: req,
            thread_id: thread_id.clone(),
            turn,
        },
    );
    (
        StatusCode::ACCEPTED,
        Json(AgentSessionStarted {
            session_id,
            thread_id,
            turn,
        }),
    )
        .into_response()
}

fn now_ms_utc() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

pub async fn stream(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> Response {
    let Some(pending) = state.agent_sessions.take(&session_id) else {
        return (StatusCode::NOT_FOUND, "session not found or already consumed").into_response();
    };
    if state.agent_client.is_none() {
        return (StatusCode::SERVICE_UNAVAILABLE, "agent disabled").into_response();
    }

    let started = std::time::Instant::now();
    let session_id_for_done = session_id.clone();

    // Hand off to the loop driver. It returns the receiver side of an
    // mpsc channel into which Progress + Claim frames flow.
    let rx = loop_driver::run_session(
        state,
        pending.request,
        session_id,
        pending.thread_id,
        pending.turn,
        started_ms(),
    );

    // Map SseFrame -> Event. After the channel closes, append a Done.
    let frames = ReceiverStream::new(rx).map(frame_to_event);
    let done = futures_util::stream::once(async move {
        let elapsed = started.elapsed().as_millis().min(u32::MAX as u128) as u32;
        let done = AgentDone {
            session_id: session_id_for_done,
            elapsed_ms: elapsed,
        };
        Ok::<Event, Infallible>(done_to_event(&done))
    });

    let combined = frames.chain(done);

    Sse::new(combined)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
        .into_response()
}

fn frame_to_event(frame: SseFrame) -> Result<Event, Infallible> {
    let ev = match &frame {
        SseFrame::Claim(claim) => match serde_json::to_string(claim) {
            Ok(json) => Event::default().event("Claim").data(json),
            Err(e) => Event::default()
                .event("error")
                .data(format!("serialize Claim failed: {e}")),
        },
        SseFrame::Progress { phase, detail } => match serde_json::to_string(&serde_json::json!({
            "phase": phase,
            "detail": detail,
        })) {
            Ok(json) => Event::default().event("Progress").data(json),
            Err(e) => Event::default()
                .event("error")
                .data(format!("serialize Progress failed: {e}")),
        },
        SseFrame::Narrative { text } => match serde_json::to_string(&serde_json::json!({
            "text": text,
        })) {
            Ok(json) => Event::default().event("Narrative").data(json),
            Err(e) => Event::default()
                .event("error")
                .data(format!("serialize Narrative failed: {e}")),
        },
        SseFrame::NarrativeRetracted {
            text,
            reason,
            debug_reason,
        } => {
            let mut payload = serde_json::json!({
                "text": text,
                "reason": reason,
            });
            // Ship 2.6.1: only include debug field on the wire when
            // backend was started with AGENT_DEBUG_PUBLIC=1. None
            // omits the field entirely so a curl of the SSE endpoint
            // in prod can never see internals.
            if let Some(d) = debug_reason {
                payload["debug_reason"] = serde_json::Value::String(d.clone());
            }
            match serde_json::to_string(&payload) {
                Ok(json) => Event::default().event("NarrativeRetracted").data(json),
                Err(e) => Event::default()
                    .event("error")
                    .data(format!("serialize NarrativeRetracted failed: {e}")),
            }
        }
        SseFrame::Error {
            message,
            debug_message,
        } => {
            let mut payload = serde_json::json!({ "message": message });
            if let Some(d) = debug_message {
                payload["debug_message"] = serde_json::Value::String(d.clone());
            }
            match serde_json::to_string(&payload) {
                Ok(json) => Event::default().event("Error").data(json),
                Err(e) => Event::default()
                    .event("error")
                    .data(format!("serialize Error failed: {e}")),
            }
        }
    };
    Ok(ev)
}

fn done_to_event(done: &AgentDone) -> Event {
    match serde_json::to_string(done) {
        Ok(json) => Event::default().event("Done").data(json),
        Err(e) => Event::default()
            .event("error")
            .data(format!("serialize Done failed: {e}")),
    }
}

fn generate_session_id() -> String {
    let mut bytes = [0u8; 16];
    for b in bytes.iter_mut() {
        *b = fastrand::u8(..);
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn started_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

