//! ReAct-style loop driver. Per-session.
//!
//! Driven by the SSE handler: builds rig adapter tools that wrap our
//! `Primitive` trait, runs `Agent::prompt(...).max_turns(N)` to let
//! rig handle the model-tool-call cycle, writes session-level ledger
//! events at start/end, and pushes Progress + Claim frames into the
//! per-session SSE sink.
//!
//! The adapter (`PrimitiveAdapter`) implements rig's `ToolDyn` trait
//! per-instance. Each registered primitive becomes one adapter; the
//! adapter calls back into `state.agent_ledger`, `state.agent_policy`,
//! `state.agent_budget`, `state.agent_stubs` so future ships swap
//! those impls without touching the loop.

use std::pin::Pin;
use std::sync::Arc;

use anyhow::Result;
use rig::client::CompletionClient;
use rig::completion::{Prompt, ToolDefinition};
use rig::message::{Message, UserContent};
use rig::providers::openrouter;
use rig::tool::{ToolDyn, ToolError};
use rig::OneOrMany;
use sha2::{Digest, Sha256};
use tokio::sync::mpsc;
use tracing::{info, warn};

use super::client::AgentClient;
use super::ledger::{LedgerEventDraft, LedgerEventKind};
use super::primitives::{
    ClaimSink, DispatchOutput, ErasedPrimitive, PrimitiveCtx, PrimitiveError, SseFrame,
};
use super::prompt::active_prompt;
use super::types::{AgentRequest, CostClass};
use crate::state::AppState;

const MAX_TURNS: usize = 8;

/// Spawn the loop for one session-turn. Returns the receiver side of
/// the SSE channel; the SSE handler subscribes to it and serializes
/// each frame as an SSE event.
///
/// `thread_id` + `turn` thread the conversation continuity layer (ship
/// 1.5): the loop loads `thread.messages` as `chat()` history, runs
/// the turn, and appends the new user prompt + final assistant text
/// back to the thread on completion.
pub fn run_session(
    state: AppState,
    request: AgentRequest,
    session_id: String,
    thread_id: String,
    turn: u32,
    session_started_at_ms: u64,
) -> mpsc::Receiver<SseFrame> {
    let (tx, rx) = mpsc::channel::<SseFrame>(32);
    tokio::spawn(async move {
        let tx_for_error = tx.clone();
        if let Err(e) = run(
            state,
            request,
            session_id.clone(),
            thread_id,
            turn,
            session_started_at_ms,
            tx,
        )
        .await
        {
            warn!(error = %e, %session_id, "agent loop errored");
            // Surface the failure to the SSE stream so the frontend
            // can finalize the pending turn instead of hanging on
            // "thinking..." until the user reloads. Best-effort; if
            // the receiver already dropped (client navigated away),
            // there's nothing to do.
            let _ = tx_for_error
                .send(SseFrame::Error {
                    message: e.to_string(),
                })
                .await;
        }
    });
    rx
}

async fn run(
    state: AppState,
    request: AgentRequest,
    session_id: String,
    thread_id: String,
    turn: u32,
    session_started_at_ms: u64,
    sse: ClaimSink,
) -> Result<()> {
    let principal_hash: [u8; 32] = [0u8; 32]; // ship 4 fills

    // ----- session ledger writes (start) ----------------------------------
    let session_payload = serde_json::json!({
        "user_question": request.user_question,
        "context": request.context,
    })
    .to_string();
    let _ = state
        .agent_ledger
        .write(LedgerEventDraft {
            session_id: session_id.clone(),
            kind: LedgerEventKind::SessionStarted,
            principal_hash,
            payload: session_payload,
            pre_estimate_units: 0,
            post_actual_units: 0,
            cost_relevant: false,
        })
        .await;

    let (prompt_tag, prompt_text) = active_prompt();
    let prompt_hash = sha256_hex(prompt_text.as_bytes());
    let _ = state
        .agent_ledger
        .write(LedgerEventDraft {
            session_id: session_id.clone(),
            kind: LedgerEventKind::Prompt,
            principal_hash,
            payload: serde_json::json!({
                "version": prompt_tag,
                "hash": prompt_hash,
            })
            .to_string(),
            pre_estimate_units: 0,
            post_actual_units: 0,
            cost_relevant: false,
        })
        .await;

    // ----- assemble user message with <context> block ---------------------
    let context_json = serde_json::to_string_pretty(&request.context)
        .unwrap_or_else(|_| "{}".into());
    let user_msg = format!(
        "<context>\n{}\n</context>\n\nQuestion: {}",
        context_json, request.user_question
    );

    // ----- pre-flight budget (stub allows) --------------------------------
    let _ = state.agent_budget.check_pre(
        &principal_hash,
        CostClass::Moderate,
        super::budget::BudgetAxis::Tokens,
        2_000,
    );

    // ----- "planning" Progress event --------------------------------------
    let _ = sse
        .send(SseFrame::Progress {
            phase: "planning".into(),
            detail: "reading context, choosing primitive".into(),
        })
        .await;

    // ----- build rig agent with our primitives wrapped as ToolDyn ---------
    let adapters = build_adapters(
        &state,
        session_id.clone(),
        principal_hash,
        session_started_at_ms,
        sse.clone(),
    );

    let started = std::time::Instant::now();
    let _ = session_started_at_ms; // also handed to adapters via state lookup

    // Pull thread history under the lock, clone, drop the lock before
    // the LLM call. v1.5 in-memory only; orphan-protected because the
    // POST handler always inserts a thread before the SSE GET runs.
    let history = {
        let threads = state.agent_threads.lock();
        threads
            .get(&thread_id)
            .map(|t| t.messages.clone())
            .unwrap_or_default()
    };
    if !history.is_empty() {
        // Follow-up turn: surface the visibility stub.
        state.agent_stubs.hit("thread.in_memory_only");
    }

    let result = match &state.agent_client {
        Some(AgentClient::OpenRouter {
            client,
            primary_model,
        }) => {
            run_with_openrouter(
                client,
                primary_model,
                prompt_text,
                &user_msg,
                history,
                adapters,
                &session_id,
                &thread_id,
                &state,
                principal_hash,
            )
            .await
        }
        None => {
            warn!("agent_client unset; cannot run loop");
            Err(anyhow::anyhow!("agent disabled"))
        }
    };

    // Append this turn's user prompt + final assistant text to the
    // thread so the next follow-up sees them. Skipped on error so a
    // failed turn doesn't pollute the thread.
    if let Ok(final_text) = &result {
        let mut threads = state.agent_threads.lock();
        if let Some(thread) = threads.get_mut(&thread_id) {
            thread
                .messages
                .push(Message::User {
                    content: OneOrMany::one(UserContent::text(user_msg.clone())),
                });
            thread.messages.push(Message::Assistant {
                id: None,
                content: OneOrMany::one(rig::message::AssistantContent::text(
                    final_text.clone(),
                )),
            });
            thread.turn_count = turn.saturating_add(1);
        }
    }

    // ----- "synthesizing" Progress (best effort) --------------------------
    let _ = sse
        .send(SseFrame::Progress {
            phase: "synthesizing".into(),
            detail: "wrapping up".into(),
        })
        .await;

    // ----- post-actual budget settle (stub no-op) -------------------------
    state.agent_budget.record_post(
        &principal_hash,
        super::budget::BudgetAxis::Tokens,
        0,
    );

    // ----- ledger session end --------------------------------------------
    let elapsed_ms = started.elapsed().as_millis().min(u32::MAX as u128) as u32;
    let session_end_payload = serde_json::json!({
        "elapsed_ms": elapsed_ms,
        "ok": result.is_ok(),
        "thread_id": thread_id,
        "turn": turn,
    })
    .to_string();
    let _ = state
        .agent_ledger
        .write(LedgerEventDraft {
            session_id: session_id.clone(),
            kind: LedgerEventKind::SessionEnded,
            principal_hash,
            payload: session_end_payload,
            pre_estimate_units: 0,
            post_actual_units: 0,
            cost_relevant: false,
        })
        .await;
    state.agent_ledger.drop_session(&session_id);
    info!(%session_id, %thread_id, turn, elapsed_ms, "agent session done");

    result.map(|_| ())
}

async fn run_with_openrouter(
    client: &openrouter::Client,
    model: &str,
    preamble: &str,
    user_msg: &str,
    history: Vec<Message>,
    adapters: Vec<Box<dyn ToolDyn>>,
    session_id: &str,
    thread_id: &str,
    state: &AppState,
    principal_hash: [u8; 32],
) -> Result<String> {
    let agent = client
        .agent(model)
        .preamble(preamble)
        .tools(adapters)
        .build();

    // Per-call observability hook: fires before/after every provider
    // hit so the logs show whether a session is in a tight tool-call
    // loop. Replaces the outer LlmCall/LlmResponse pair (which only
    // saw the wrap of the whole multi-turn loop, not individual hits).
    // rig's openrouter `Client` exposes `base_url()` (e.g.
    // "https://openrouter.ai/api/v1") but not the full per-call URL.
    // The chat endpoint path is fixed inside rig
    // (`/chat/completions` in providers/openrouter/completion.rs),
    // so we compose the full URL ourselves for log visibility. If we
    // ever swap providers or rig adds a per-call URL hook, this is
    // the one place to update.
    let endpoint = format!("{}/chat/completions", client.base_url());
    let logger = super::hooks::LlmCallLogger::new(
        session_id.to_string(),
        thread_id.to_string(),
        model.to_string(),
        endpoint,
        state.clone(),
        principal_hash,
    );

    // rig 0.36: `Chat::chat()` is single-turn (no tool loop) and has
    // no `.max_turns()`. The right API for tool-using multi-turn with
    // history is `prompt(user).with_history(...).max_turns(N)`.
    // `.max_turns()` is required: rig defaults to 0 multi-turns,
    // which trips after the first round of tool calls.
    let response_text = agent
        .prompt(user_msg)
        .with_history(history)
        .with_hook(logger)
        .max_turns(MAX_TURNS)
        .await
        .map_err(|e| anyhow::anyhow!("rig prompt failed: {e}"))?;

    Ok(response_text)
}

fn build_adapters(
    state: &AppState,
    session_id: String,
    principal_hash: [u8; 32],
    session_started_at_ms: u64,
    sse: ClaimSink,
) -> Vec<Box<dyn ToolDyn>> {
    let primitives = state.agent_registry.all();
    primitives
        .into_iter()
        .map(|p| -> Box<dyn ToolDyn> {
            Box::new(PrimitiveAdapter {
                primitive: p,
                state: state.clone(),
                session_id: session_id.clone(),
                principal_hash,
                session_started_at_ms,
                sse: sse.clone(),
            })
        })
        .collect()
}

/// Wraps any `ErasedPrimitive` as a rig `ToolDyn`. One adapter type
/// covers every primitive in the registry; ship 3+ register more
/// primitives without writing more adapters.
struct PrimitiveAdapter {
    primitive: Arc<dyn ErasedPrimitive>,
    state: AppState,
    session_id: String,
    principal_hash: [u8; 32],
    session_started_at_ms: u64,
    sse: ClaimSink,
}

impl ToolDyn for PrimitiveAdapter {
    fn name(&self) -> String {
        self.primitive.name().to_string()
    }

    fn definition<'a>(
        &'a self,
        _prompt: String,
    ) -> Pin<Box<dyn std::future::Future<Output = ToolDefinition> + Send + 'a>> {
        Box::pin(async move {
            ToolDefinition {
                name: self.primitive.name().to_string(),
                description: self.primitive.description().to_string(),
                parameters: self.primitive.input_schema(),
            }
        })
    }

    fn call<'a>(
        &'a self,
        args: String,
    ) -> Pin<Box<dyn std::future::Future<Output = Result<String, ToolError>> + Send + 'a>> {
        Box::pin(async move {
            // Push a Progress event so the UI shows "tool_call ..."
            let _ = self
                .sse
                .send(SseFrame::Progress {
                    phase: "tool_call".into(),
                    detail: self.primitive.name().to_string(),
                })
                .await;

            // Pre-flight budget (stub allows).
            let _ = self.state.agent_budget.check_pre(
                &self.principal_hash,
                self.primitive.cost_class(),
                super::budget::BudgetAxis::DbTimeMs,
                10,
            );

            // Parse args once for both ledger and dispatch.
            let parsed: serde_json::Value = serde_json::from_str(&args).unwrap_or_else(|_| serde_json::Value::Null);

            // Ledger ToolCall.
            let _ = self
                .state
                .agent_ledger
                .write(LedgerEventDraft {
                    session_id: self.session_id.clone(),
                    kind: LedgerEventKind::ToolCall,
                    principal_hash: self.principal_hash,
                    payload: serde_json::json!({
                        "name": self.primitive.name(),
                        "args": &parsed,
                    })
                    .to_string(),
                    pre_estimate_units: 0,
                    post_actual_units: 0,
                    cost_relevant: true,
                })
                .await;

            let ctx = PrimitiveCtx {
                state: &self.state,
                session_id: self.session_id.clone(),
                principal_hash: self.principal_hash,
                session_started_at_ms: self.session_started_at_ms,
                sse: self.sse.clone(),
            };

            let dispatch_result = self
                .primitive
                .execute_erased(&ctx, parsed)
                .await;

            // Settle budget (stub no-op).
            self.state.agent_budget.record_post(
                &self.principal_hash,
                super::budget::BudgetAxis::DbTimeMs,
                0,
            );

            // Ledger ToolResult + return.
            match dispatch_result {
                Ok(DispatchOutput { value_json, .. }) => {
                    let result_str = value_json.to_string();
                    let _ = self
                        .state
                        .agent_ledger
                        .write(LedgerEventDraft {
                            session_id: self.session_id.clone(),
                            kind: LedgerEventKind::ToolResult,
                            principal_hash: self.principal_hash,
                            payload: serde_json::json!({
                                "name": self.primitive.name(),
                                "ok": true,
                                "size": result_str.len(),
                            })
                            .to_string(),
                            pre_estimate_units: 0,
                            post_actual_units: 0,
                            cost_relevant: true,
                        })
                        .await;
                    Ok(result_str)
                }
                Err(e) => {
                    let err_str = e.to_string();
                    let _ = self
                        .state
                        .agent_ledger
                        .write(LedgerEventDraft {
                            session_id: self.session_id.clone(),
                            kind: LedgerEventKind::ToolResult,
                            principal_hash: self.principal_hash,
                            payload: serde_json::json!({
                                "name": self.primitive.name(),
                                "ok": false,
                                "error": err_str,
                            })
                            .to_string(),
                            pre_estimate_units: 0,
                            post_actual_units: 0,
                            cost_relevant: true,
                        })
                        .await;
                    // Recoverable PrimitiveErrors come back to the
                    // model as tool results so it can react. We
                    // surface them as Ok with an error JSON; rig's
                    // ToolError otherwise drops the loop.
                    match e {
                        PrimitiveError::NotInWindow { .. }
                        | PrimitiveError::NotImplemented { .. }
                        | PrimitiveError::InvalidInput { .. } => Ok(serde_json::json!({
                            "error": err_str,
                        })
                        .to_string()),
                        PrimitiveError::Internal(_) => Err(ToolError::ToolCallError(Box::new(
                            std::io::Error::new(std::io::ErrorKind::Other, err_str),
                        ))),
                    }
                }
            }
        })
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    let digest: [u8; 32] = h.finalize().into();
    super::ledger::event::hex_encode(&digest)
}
