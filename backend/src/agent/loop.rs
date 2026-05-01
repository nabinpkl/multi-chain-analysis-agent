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
    build_binding, ClaimSink, DispatchOutput, ErasedPrimitive, PrimitiveBindingStore, PrimitiveCtx,
    PrimitiveError, SseFrame,
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
    // Capture before moving `state` into `run`. The error path below
    // needs to know whether to populate the wire's debug_message
    // field; AppState is gone by the time we get there.
    let debug_public = state.agent_debug_public;
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
            let raw = e.to_string();
            warn!(error = %raw, %session_id, "agent loop errored");
            // Surface the failure to the SSE stream so the frontend
            // can finalize the pending turn instead of hanging on
            // "thinking..." until the user reloads. The user-facing
            // `message` is always generic; the raw rig error
            // (provider name, status code, upstream user_id, etc.)
            // only goes on the wire when AGENT_DEBUG_PUBLIC=1.
            let _ = tx_for_error
                .send(SseFrame::Error {
                    message:
                        "Couldn't produce a valid response. Try rephrasing or try again."
                            .to_string(),
                    debug_message: if debug_public { Some(raw) } else { None },
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

    // Per-attempt: tool adapters are rebuilt inside the retry loop
    // (rig consumes `Vec<Box<dyn ToolDyn>>` per `agent.prompt(...)`,
    // and we may run multiple attempts per turn).
    let started = std::time::Instant::now();

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

    // Snapshot prior-turn Claims for lenient-mode cross-check. Cloned
    // under brief lock so the gate runs without holding it. Loop-
    // invariant across attempts.
    let thread_history_claims: Vec<crate::agent::types::Claim> = state
        .agent_threads
        .lock()
        .get(&thread_id)
        .map(|t| t.claims.clone())
        .unwrap_or_default();

    // Ship 3: prime the per-session binding buffer from the thread's
    // persistent store. Each primitive dispatch in this turn appends
    // here (via the tool adapter); the policy gate's binding leg
    // reads from this buffer; session-end writes the buffer back to
    // the thread.
    let initial_bindings = state
        .agent_threads
        .lock()
        .get(&thread_id)
        .map(|t| t.bindings.clone())
        .unwrap_or_default();
    {
        let mut buf = state.agent_bindings.lock();
        buf.insert(session_id.clone(), initial_bindings);
    }

    // ----- ship 2.6 narrative-retry loop ----------------------------------
    //
    // Up to MAX_NARRATIVE_ATTEMPTS rig.prompt() calls per turn. Retry
    // fires when the narrative gate retracts AND no Claim has yet
    // been pushed to SSE (Claims commit on emit_claim and we cannot
    // un-push them). On retract with Claims already emitted, the
    // turn stands as "Claim card + retracted narrative". On retract
    // with no Claims and attempts exhausted, we send `SseFrame::Error`
    // with a generic friendly message so the user doesn't see the
    // policy reason verbatim (constitution rules are a defense-layer
    // concern, not user UX).
    //
    // Across retries: rig adapters and tools are reused (one
    // primitive registry, one ToolDyn vec). Each retry extends the
    // conversation history with the previous attempt's text + a
    // retry-feedback user message naming the constitution rule that
    // tripped, so the model can self-correct.
    const MAX_NARRATIVE_ATTEMPTS: u32 = 3;
    let mut attempt: u32 = 0;
    let mut retry_extension: Vec<Message> = Vec::new();
    let mut prompt_for_attempt: String = user_msg.clone();
    let mut accumulated_claims: Vec<crate::agent::types::Claim> = Vec::new();
    let mut last_text: Option<String> = None;
    // Track the most recent retract reason so the friendly-error
    // path can attach it as `debug_message` in dev mode (ship 2.6.1).
    // Cleared on approve.
    let mut last_retract_reason: Option<String> = None;
    let debug_public = state.agent_debug_public;

    let result = loop {
        // Build the rig invocation per-attempt: history is base
        // (thread.messages) + retry_extension. The "user message"
        // is `prompt_for_attempt`, which is the original wrapped
        // question on attempt 0 and a retry-feedback message on
        // subsequent attempts.
        let extended_history: Vec<Message> = history
            .iter()
            .cloned()
            .chain(retry_extension.iter().cloned())
            .collect();

        let attempt_result = match &state.agent_client {
            Some(AgentClient::OpenRouter {
                client,
                primary_model,
                ..
            }) => {
                // We rebuild adapters per-attempt to keep one
                // ToolDyn instance per call (rig consumes them).
                let adapters_for_attempt = build_adapters(
                    &state,
                    session_id.clone(),
                    principal_hash,
                    session_started_at_ms,
                    sse.clone(),
                );
                run_with_openrouter(
                    client,
                    primary_model,
                    prompt_text,
                    &prompt_for_attempt,
                    extended_history,
                    adapters_for_attempt,
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

        let attempt_text = match &attempt_result {
            Ok(t) => t.clone(),
            Err(e) => {
                // Provider-level failure (502, network, etc.). Bail
                // out of the retry loop entirely; the outer error
                // path emits SseFrame::Error with the friendly text
                // + raw error in debug field when dev-mode is on.
                // Pass the inner error verbatim  `run_with_openrouter`
                // already prefixed "rig prompt failed:", so wrapping
                // again would produce "rig prompt failed: rig prompt
                // failed: ..." (caught in dogfood; ship 2.6.1 fix).
                break Err(anyhow::anyhow!("{e}"));
            }
        };
        last_text = Some(attempt_text.clone());

        // Drain Claims emitted during THIS attempt. emit_claim
        // already pushed them to SSE; we just track for the gate's
        // reference set + thread persistence.
        let new_claims: Vec<crate::agent::types::Claim> = state
            .agent_claims_emitted
            .lock()
            .remove(&session_id)
            .unwrap_or_default();
        accumulated_claims.extend(new_claims.iter().cloned());

        let trimmed = attempt_text.trim();

        // Empty narrative: no prose to gate. If Claims emitted, the
        // turn is "Claim only"  accept. If no Claims either, retry
        // (model may have produced nothing useful; retrying nudges
        // it toward a real answer).
        if trimmed.is_empty() {
            if !accumulated_claims.is_empty() {
                break Ok(()); // claim-only turn, no narrative needed
            }
            if attempt + 1 < MAX_NARRATIVE_ATTEMPTS {
                let _ = sse
                    .send(SseFrame::Progress {
                        phase: "retrying".into(),
                        detail: format!(
                            "no response on attempt {} of {}",
                            attempt + 1,
                            MAX_NARRATIVE_ATTEMPTS
                        ),
                    })
                    .await;
                retry_extension.push(Message::User {
                    content: OneOrMany::one(UserContent::text(prompt_for_attempt.clone())),
                });
                retry_extension.push(Message::Assistant {
                    id: None,
                    content: OneOrMany::one(rig::message::AssistantContent::text(
                        attempt_text.clone(),
                    )),
                });
                prompt_for_attempt = "Your previous response was empty. Please answer the user's question with either a Claim (via emit_claim) or interpretive narrative.".to_string();
                attempt += 1;
                continue;
            }
            // Exhausted: friendly error, no specifics.
            // Dev-mode debug field carries the last retract reason
            // (or "agent produced no response" if we never hit the
            // gate). Production wire stays sterile.
            let debug = last_retract_reason
                .as_deref()
                .or(Some("agent produced empty narrative on every attempt"));
            send_friendly_error(&sse, debug_public, debug).await;
            break Ok(());
        }

        // Run the three-verdict narrative gate (ship 2.7). Reference
        // set = accumulated claims (this turn, all attempts) + thread
        // history. Returns merged verdict + per-leg breakdown +
        // raw LLM extraction (for ledger replay).
        info!(
            %session_id,
            %thread_id,
            turn,
            attempt,
            len = trimmed.len(),
            same_turn_claims = accumulated_claims.len(),
            thread_history_claims = thread_history_claims.len(),
            "narrative emitted; gating",
        );
        let binding_snapshot = state
            .agent_bindings
            .lock()
            .get(&session_id)
            .cloned()
            .unwrap_or_default();
        let gate_result = state
            .agent_policy
            .check_narrative(
                trimmed,
                &accumulated_claims,
                &thread_history_claims,
                &binding_snapshot,
            )
            .await;
        let verdict = gate_result.verdict.clone();
        let breakdown = gate_result.breakdown.clone();
        let raw_extraction = gate_result.raw_extraction.clone();
        let breakdown_dev_str = breakdown.format_for_dev();

        // Ledger PolicyVerdict for this attempt  extended with the
        // per-leg breakdown + raw extraction so ship 6's eval can
        // assert per-extractor correctness on golden questions. Ship
        // 3 adds `binding_call_ids` so adversarial replay can ask
        // "what primitives did this verdict's binding leg see".
        let verdict_payload = serde_json::json!({
            "target": "narrative",
            "verdict": &verdict,
            "thread_id": &thread_id,
            "turn": turn,
            "attempt": attempt,
            "breakdown": &breakdown,
            "raw_extraction": &raw_extraction,
            "binding_call_ids": binding_snapshot.call_ids(),
        })
        .to_string();
        let _ = state
            .agent_ledger
            .write(super::ledger::LedgerEventDraft {
                session_id: session_id.clone(),
                kind: super::ledger::LedgerEventKind::PolicyVerdict,
                principal_hash,
                payload: verdict_payload,
                pre_estimate_units: 0,
                post_actual_units: 0,
                cost_relevant: false,
            })
            .await;

        match verdict {
            crate::agent::types::PolicyVerdict::Approved => {
                last_retract_reason = None;
                let _ = sse
                    .send(SseFrame::Narrative {
                        text: trimmed.to_string(),
                    })
                    .await;
                break Ok(());
            }
            crate::agent::types::PolicyVerdict::Retracted { reason } => {
                last_retract_reason = Some(reason.clone());
                info!(
                    %session_id,
                    %thread_id,
                    attempt,
                    reason = %reason,
                    breakdown = %breakdown_dev_str,
                    claims_already_emitted = accumulated_claims.len(),
                    "narrative retracted by policy",
                );

                // Retry only when no Claim has flowed to SSE yet.
                // emit_claim pushes Claim frames on every call, so
                // any Claim from a prior attempt is already on the
                // wire  retrying would add a duplicate.
                let claims_committed = !accumulated_claims.is_empty();
                let attempts_remaining = attempt + 1 < MAX_NARRATIVE_ATTEMPTS;

                if !claims_committed && attempts_remaining {
                    let _ = sse
                        .send(SseFrame::Progress {
                            phase: "retrying".into(),
                            detail: format!(
                                "policy retracted; refining (attempt {} of {})",
                                attempt + 2,
                                MAX_NARRATIVE_ATTEMPTS
                            ),
                        })
                        .await;
                    retry_extension.push(Message::User {
                        content: OneOrMany::one(UserContent::text(prompt_for_attempt.clone())),
                    });
                    retry_extension.push(Message::Assistant {
                        id: None,
                        content: OneOrMany::one(rig::message::AssistantContent::text(
                            attempt_text.clone(),
                        )),
                    });
                    // The retry-feedback message names the rules
                    // that tripped (server-side; the user never sees
                    // this) so the model can self-correct without
                    // rewriting prompt v2 every iteration.
                    prompt_for_attempt = format!(
                        "Your previous response was retracted by the output policy. Reason: {reason}. Retry the user's question, this time staying within the rules: numbers in narrative must come from cited Claims (this turn or prior turns of the thread); do not compute new numbers; stay in domain (Solana on-chain graph analysis); decline politely if the question is out of scope; do not name the underlying LLM."
                    );
                    attempt += 1;
                    continue;
                }

                // No retry path. Two cases:
                if claims_committed {
                    // Claim card carries the answer; surface
                    // narrative as retracted so the user sees policy
                    // intervened on the prose. Friendly `reason`
                    // positions the Claim above as the real output;
                    // the dev-mode `debug_reason` carries the
                    // three-extractor breakdown so disagreement is
                    // visible inline.
                    let _ = sse
                        .send(SseFrame::NarrativeRetracted {
                            text: trimmed.to_string(),
                            reason:
                                "Interpretation withheld; the structured profile above carries the verifiable answer."
                                    .to_string(),
                            debug_reason: if debug_public {
                                Some(breakdown_dev_str.clone())
                            } else {
                                None
                            },
                        })
                        .await;
                } else {
                    // Exhausted retries with nothing to show.
                    // Friendly error, no specifics. Dev-mode field
                    // carries the breakdown for inline visibility.
                    send_friendly_error(
                        &sse,
                        debug_public,
                        Some(breakdown_dev_str.as_str()),
                    )
                    .await;
                }
                break Ok(());
            }
        }
    };

    // Append this turn to thread.messages so future follow-ups have
    // context. We save the LAST attempt's text (even if retracted)
    // because the conversation history is for raw context, not gated
    // content; hiding turn 0 entirely would leave the model unable to
    // resolve "it" / "this" on a turn-1 follow-up. Also persist any
    // approved Claims onto thread.claims for the next narrative
    // gate's lenient reference set.
    // Ship 3: pull the per-session binding buffer out before we
    // touch the thread map so we can fold it back into thread state
    // under the same lock as the message / claim updates.
    let final_bindings = state.agent_bindings.lock().remove(&session_id);

    if result.is_ok() {
        let mut threads = state.agent_threads.lock();
        if let Some(thread) = threads.get_mut(&thread_id) {
            if let Some(text) = last_text.as_ref() {
                thread.messages.push(Message::User {
                    content: OneOrMany::one(UserContent::text(user_msg.clone())),
                });
                thread.messages.push(Message::Assistant {
                    id: None,
                    content: OneOrMany::one(rig::message::AssistantContent::text(text.clone())),
                });
            }
            thread.turn_count = turn.saturating_add(1);
            if !accumulated_claims.is_empty() {
                thread.claims.extend(accumulated_claims.iter().cloned());
                if thread.claims.len() > super::MAX_THREAD_CLAIMS {
                    let drop = thread.claims.len() - super::MAX_THREAD_CLAIMS;
                    thread.claims.drain(0..drop);
                }
            }
            // Ship 3: persist the binding store back. The buffer
            // includes any new bindings recorded during this turn
            // so a follow-up turn's binding leg can validate
            // against them. Ring buffer eviction (cap
            // MAX_THREAD_BINDINGS) already happened in `record`.
            if let Some(bindings) = final_bindings {
                thread.bindings = bindings;
            }
        }
    } else {
        // Failed turn: still drop the per-session buffer so it
        // doesn't leak across sessions. Already removed above; the
        // thread's persistent bindings stay as they were before
        // this turn started.
        drop(final_bindings);
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
                Ok(DispatchOutput {
                    value_json,
                    provenance,
                    ..
                }) => {
                    // Ship 3: record a binding for the per-session
                    // buffer. emit_claim is itself a primitive but
                    // its provenance is empty + its output is a
                    // `{claim_id, policy}` object that contains no
                    // audit-class numbers; recording it is harmless
                    // and keeps the path uniform. Future ships can
                    // skip emit_claim explicitly if the noise hurts.
                    let captured_at_ms = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_millis() as u64)
                        .unwrap_or(0);
                    let call_id = format!(
                        "{}:{}",
                        self.primitive.name(),
                        ulid::Ulid::new()
                    );
                    let binding = build_binding(
                        self.primitive.name(),
                        call_id,
                        captured_at_ms,
                        &value_json,
                        &provenance,
                    );
                    {
                        let mut buf = self.state.agent_bindings.lock();
                        buf.entry(self.session_id.clone())
                            .or_insert_with(PrimitiveBindingStore::new)
                            .record(binding);
                    }

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

/// Generic user-facing error after exhausting narrative retries
/// (ship 2.6). Deliberately category-free: leaking which constitution
/// rule retracted means leaking the gate's shape, which is a defense
/// concern. The full retract reason is in the ledger
/// (`PolicyVerdict` rows per attempt) for ops debugging.
///
/// Ship 2.6.1: when `debug_public` is true, `last_reason` is attached
/// to the wire as `debug_message` so the dev-mode UI can surface it
/// inline without leaking to prod users.
async fn send_friendly_error(sse: &ClaimSink, debug_public: bool, last_reason: Option<&str>) {
    let _ = sse
        .send(SseFrame::Error {
            message:
                "Couldn't produce a valid response. Try rephrasing or try again."
                    .to_string(),
            debug_message: if debug_public {
                last_reason.map(|r| r.to_string())
            } else {
                None
            },
        })
        .await;
}
