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
use rig::providers::openrouter;
use rig::tool::{ToolDyn, ToolError};
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

/// Spawn the loop for one session. Returns the receiver side of the
/// SSE channel; the SSE handler subscribes to it and serializes each
/// frame as an SSE event.
pub fn run_session(
    state: AppState,
    request: AgentRequest,
    session_id: String,
    session_started_at_ms: u64,
) -> mpsc::Receiver<SseFrame> {
    let (tx, rx) = mpsc::channel::<SseFrame>(32);
    tokio::spawn(async move {
        if let Err(e) = run(state, request, session_id.clone(), session_started_at_ms, tx).await {
            warn!(error = %e, %session_id, "agent loop errored");
        }
    });
    rx
}

async fn run(
    state: AppState,
    request: AgentRequest,
    session_id: String,
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
                adapters,
                &session_id,
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
    info!(%session_id, elapsed_ms, "agent session done");

    result.map(|_| ())
}

async fn run_with_openrouter(
    client: &openrouter::Client,
    model: &str,
    preamble: &str,
    user_msg: &str,
    adapters: Vec<Box<dyn ToolDyn>>,
    session_id: &str,
    state: &AppState,
    principal_hash: [u8; 32],
) -> Result<()> {
    // rig's `Agent` builder wants tools added one at a time via
    // `.tool()` (typed) or as a vec via the underlying API. We use
    // `dyn_tool()` which accepts boxed ToolDyn so heterogeneous
    // primitives register through one path.
    let agent = client
        .agent(model)
        .preamble(preamble)
        .tools(adapters)
        .build();

    // Write LlmCall ledger event before invoking. We don't have token
    // counts in v0 (rig's Agent::prompt doesn't surface usage on this
    // path); ship 4 records actual usage from the lower-level model
    // call path.
    let _ = state
        .agent_ledger
        .write(LedgerEventDraft {
            session_id: session_id.to_string(),
            kind: LedgerEventKind::LlmCall,
            principal_hash,
            payload: serde_json::json!({
                "model": model,
                "max_turns": MAX_TURNS,
            })
            .to_string(),
            pre_estimate_units: 0,
            post_actual_units: 0,
            cost_relevant: false,
        })
        .await;

    let response_text = agent
        .prompt(user_msg)
        .max_turns(MAX_TURNS)
        .await
        .map_err(|e| anyhow::anyhow!("rig prompt failed: {e}"))?;

    let _ = state
        .agent_ledger
        .write(LedgerEventDraft {
            session_id: session_id.to_string(),
            kind: LedgerEventKind::LlmResponse,
            principal_hash,
            payload: serde_json::json!({
                "final_text_len": response_text.len(),
            })
            .to_string(),
            pre_estimate_units: 0,
            post_actual_units: 0,
            cost_relevant: false,
        })
        .await;

    Ok(())
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
