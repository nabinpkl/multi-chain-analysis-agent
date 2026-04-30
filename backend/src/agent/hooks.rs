//! Per-call observability hook for the rig agent loop.
//!
//! `agent.prompt(...).max_turns(N)` may hit the LLM provider up to
//! `N+1` times in a single session (once per turn of the inner tool
//! loop). Without this hook we only see the outer entry/exit pair and
//! cannot tell from logs whether a session is in a tight loop hitting
//! the provider repeatedly.
//!
//! The hook fires before and after every provider call:
//! - `on_completion_call`: writes `LlmCall` ledger event + emits an
//!   `info!` log with `(session_id, thread_id, call_index, history_len)`.
//! - `on_completion_response`: writes `LlmResponse` ledger event with
//!   token usage + `info!` log.
//!
//! Filter logs by `call_index >= 4` to spot likely-runaway sessions.
//! Once ship 4 lands, the budget framework caps these per principal.

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use rig::agent::{HookAction, PromptHook};
use rig::completion::{CompletionModel, CompletionResponse};
use rig::message::Message;
use tracing::info;

use super::ledger::{LedgerEventDraft, LedgerEventKind};
use crate::state::AppState;

/// Per-session hook handed to `prompt(...).with_hook(...)`. Cheap to
/// clone (everything Arc'd or Copy).
#[derive(Clone)]
pub struct LlmCallLogger {
    pub session_id: Arc<String>,
    pub thread_id: Arc<String>,
    pub model: Arc<String>,
    /// Full URL we hit per call. rig's openrouter provider does not
    /// expose the per-call URL via a hook, but `client.base_url()` is
    /// public and the chat path is hardcoded to `/chat/completions`
    /// (see `rig-core/src/providers/openrouter/completion.rs:1748`),
    /// so we compose it once at logger construction. If rig adds a
    /// per-call URL hook later, we can swap to that without changing
    /// the field shape.
    pub endpoint: Arc<String>,
    pub state: AppState,
    pub principal_hash: [u8; 32],
    pub call_index: Arc<AtomicU32>,
}

impl LlmCallLogger {
    pub fn new(
        session_id: String,
        thread_id: String,
        model: String,
        endpoint: String,
        state: AppState,
        principal_hash: [u8; 32],
    ) -> Self {
        Self {
            session_id: Arc::new(session_id),
            thread_id: Arc::new(thread_id),
            model: Arc::new(model),
            endpoint: Arc::new(endpoint),
            state,
            principal_hash,
            call_index: Arc::new(AtomicU32::new(0)),
        }
    }
}

impl<M> PromptHook<M> for LlmCallLogger
where
    M: CompletionModel,
{
    fn on_completion_call(
        &self,
        _prompt: &Message,
        history: &[Message],
    ) -> impl Future<Output = HookAction> + Send {
        let session_id = self.session_id.clone();
        let thread_id = self.thread_id.clone();
        let model = self.model.clone();
        let endpoint = self.endpoint.clone();
        let state = self.state.clone();
        let principal_hash = self.principal_hash;
        let call = self.call_index.fetch_add(1, Ordering::Relaxed) + 1;
        let history_len = history.len();
        async move {
            info!(
                session_id = %session_id,
                thread_id = %thread_id,
                model = %model,
                endpoint = %endpoint,
                call,
                history_len,
                "llm provider call",
            );
            let _ = state
                .agent_ledger
                .write(LedgerEventDraft {
                    session_id: (*session_id).clone(),
                    kind: LedgerEventKind::LlmCall,
                    principal_hash,
                    payload: serde_json::json!({
                        "call": call,
                        "model": *model,
                        "endpoint": *endpoint,
                        "thread_id": *thread_id,
                        "history_len": history_len,
                    })
                    .to_string(),
                    pre_estimate_units: 0,
                    post_actual_units: 0,
                    cost_relevant: true,
                })
                .await;
            HookAction::cont()
        }
    }

    fn on_completion_response(
        &self,
        _prompt: &Message,
        response: &CompletionResponse<M::Response>,
    ) -> impl Future<Output = HookAction> + Send {
        let session_id = self.session_id.clone();
        let thread_id = self.thread_id.clone();
        let endpoint = self.endpoint.clone();
        let state = self.state.clone();
        let principal_hash = self.principal_hash;
        // call_index already incremented in on_completion_call; the
        // response we're seeing is for that same call.
        let call = self.call_index.load(Ordering::Relaxed);
        let prompt_tokens = response.usage.input_tokens;
        let completion_tokens = response.usage.output_tokens;
        let total_tokens = response.usage.total_tokens;
        async move {
            info!(
                session_id = %session_id,
                thread_id = %thread_id,
                endpoint = %endpoint,
                call,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                "llm provider response",
            );
            let _ = state
                .agent_ledger
                .write(LedgerEventDraft {
                    session_id: (*session_id).clone(),
                    kind: LedgerEventKind::LlmResponse,
                    principal_hash,
                    payload: serde_json::json!({
                        "call": call,
                        "thread_id": *thread_id,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                    })
                    .to_string(),
                    pre_estimate_units: 0,
                    post_actual_units: total_tokens
                        .min(u32::MAX as u64) as u32,
                    cost_relevant: true,
                })
                .await;
            HookAction::cont()
        }
    }
}
