//! Typed primitive layer. Every analysis the agent can perform is a
//! composition of registered primitives. The trait + registry shape is
//! locked in this ship; future ships add primitives by registering
//! them, never by changing the trait.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::Result;
use async_trait::async_trait;
use schemars::{JsonSchema, schema_for};
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::Value;
use thiserror::Error;
use tokio::sync::mpsc;

use super::types::{Claim, CostClass, DataSource, ProvenanceRef, SubgraphSlice};
use crate::state::AppState;

pub mod emit_claim;
pub mod wallet_profile;

pub use emit_claim::{EmitClaimInput, EmitClaimPrimitive};
pub use wallet_profile::{WalletProfileInput, WalletProfileOutput, WalletProfilePrimitive};

/// Per-call context handed to primitive `execute`. Carries the shared
/// `AppState` (graph, clickhouse, analytics snapshots, ledger, policy,
/// stub registry) plus the per-session sink so primitives like
/// `emit_claim` can push frames to the SSE stream.
pub struct PrimitiveCtx<'a> {
    pub state: &'a AppState,
    pub session_id: String,
    /// v0: zero-array. Ship 4 fills this from cookie + truncated IP.
    pub principal_hash: [u8; 32],
    /// Wallclock ms when the session started. emit_claim subtracts this
    /// from `now()` to fill `Claim.emitted_at_ms`.
    pub session_started_at_ms: u64,
    pub sse: ClaimSink,
}

/// Streamable frames the agent emits to the SSE channel. The loop and
/// the `emit_claim` primitive both push into the sink; the SSE handler
/// subscribes on the receiving end.
#[derive(Debug, Clone)]
pub enum SseFrame {
    Claim(Claim),
    Progress { phase: String, detail: String },
    /// Free-form interpretive prose. Carries the model's natural reply
    /// text (the string returned by `rig::prompt(...).await`) so the
    /// frontend can render it as an "interpretation" bubble alongside
    /// the structured Claim cards. Ship 1.6 introduced this channel.
    /// Ship 2 added the constitution gate: only narrative that the
    /// gate approves arrives as `Narrative`; gate-retracted narrative
    /// arrives as `NarrativeRetracted` instead.
    Narrative { text: String },
    /// Narrative the constitution gate retracted (ship 2). Carries the
    /// original text alongside a friendly user-facing `reason`.
    ///
    /// `debug_reason` is the raw policy reason (e.g. "narrative
    /// number 50000 SOL not found in cited Claims"); only populated
    /// when `AGENT_DEBUG_PUBLIC=1` (ship 2.6.1 dev-mode). The
    /// frontend renders it inline as an expandable diagnostic block
    /// when present so the dev sees rare events on the UI itself.
    /// Prod default: `debug_reason: None`, wire stays sterile.
    NarrativeRetracted {
        text: String,
        reason: String,
        debug_reason: Option<String>,
    },
    /// Terminal turn-level error (e.g. provider 5xx, network drop, rig
    /// loop crashed). The SSE handler renders this as an `Error` event
    /// before the closing `Done`, so the frontend can finalize the
    /// pending turn instead of hanging on its "thinking..." spinner.
    ///
    /// `message` is the friendly user-facing string. `debug_message`
    /// carries the raw underlying error (rig prompt failure, HTTP
    /// status, etc.) only when `AGENT_DEBUG_PUBLIC=1`; absent in
    /// prod so we don't leak provider names, status codes, or
    /// upstream user_ids to end users.
    Error {
        message: String,
        debug_message: Option<String>,
    },
}

pub type ClaimSink = mpsc::Sender<SseFrame>;

/// Primitive output bundle. The agent loop reads `value` to feed back
/// to the model as a tool result; provenance + subgraph_slice flow
/// through to claim emission.
pub struct PrimitiveOutput<T> {
    pub value: T,
    pub provenance: Vec<ProvenanceRef>,
    pub subgraph_slice: Option<SubgraphSlice>,
}

#[derive(Debug, Error)]
pub enum PrimitiveError {
    #[error("invalid input: {reason}")]
    InvalidInput { reason: String },
    /// Recoverable. The agent gets the error in the tool result and
    /// can react (typically by emitting a Summary claim explaining).
    #[error("wallet not in current live window: {addr}")]
    NotInWindow { addr: String },
    /// Stub-marked path. Carries the ship that promotes it. v0
    /// surfaces this when wallet_profile is called with `Range`.
    #[error("not implemented: {reason} (lands in ship {ship})")]
    NotImplemented { reason: String, ship: u8 },
    #[error("primitive internal error: {0}")]
    Internal(#[from] anyhow::Error),
}

/// What every primitive looks like. `Input`/`Output` are JSON-schema-
/// deriving so rig sees them as typed tools the model can call.
#[async_trait]
pub trait Primitive: Send + Sync {
    type Input: DeserializeOwned + JsonSchema + Send;
    type Output: Serialize + JsonSchema + Send;

    fn name(&self) -> &'static str;
    fn description(&self) -> &'static str;
    fn data_source(&self) -> DataSource;
    fn cost_class(&self) -> CostClass;

    async fn execute(
        &self,
        ctx: &PrimitiveCtx<'_>,
        input: Self::Input,
    ) -> Result<PrimitiveOutput<Self::Output>, PrimitiveError>;
}

/// Type-erased primitive for the registry. Uses serde_json::Value as
/// the wire shape so the registry can hold heterogeneous primitives.
#[async_trait]
pub trait ErasedPrimitive: Send + Sync {
    fn name(&self) -> &'static str;
    fn description(&self) -> &'static str;
    fn data_source(&self) -> DataSource;
    fn cost_class(&self) -> CostClass;
    fn input_schema(&self) -> Value;
    async fn execute_erased(
        &self,
        ctx: &PrimitiveCtx<'_>,
        args: Value,
    ) -> Result<DispatchOutput, PrimitiveError>;
}

/// Erased per-call output: serialized value + provenance + slice.
pub struct DispatchOutput {
    pub value_json: Value,
    pub provenance: Vec<ProvenanceRef>,
    pub subgraph_slice: Option<SubgraphSlice>,
}

/// Blanket impl turns any `Primitive` into an `ErasedPrimitive`. The
/// registry stores `Arc<dyn ErasedPrimitive>`.
#[async_trait]
impl<P> ErasedPrimitive for P
where
    P: Primitive + 'static,
{
    fn name(&self) -> &'static str {
        Primitive::name(self)
    }
    fn description(&self) -> &'static str {
        Primitive::description(self)
    }
    fn data_source(&self) -> DataSource {
        Primitive::data_source(self)
    }
    fn cost_class(&self) -> CostClass {
        Primitive::cost_class(self)
    }
    fn input_schema(&self) -> Value {
        let s = schema_for!(P::Input);
        serde_json::to_value(s).unwrap_or_else(|_| Value::Object(Default::default()))
    }

    async fn execute_erased(
        &self,
        ctx: &PrimitiveCtx<'_>,
        args: Value,
    ) -> Result<DispatchOutput, PrimitiveError> {
        let input: P::Input = serde_json::from_value(args).map_err(|e| {
            PrimitiveError::InvalidInput {
                reason: format!("input schema mismatch: {e}"),
            }
        })?;
        let out = self.execute(ctx, input).await?;
        let value_json = serde_json::to_value(out.value).map_err(|e| {
            PrimitiveError::Internal(anyhow::anyhow!("output serialize: {e}"))
        })?;
        Ok(DispatchOutput {
            value_json,
            provenance: out.provenance,
            subgraph_slice: out.subgraph_slice,
        })
    }
}

/// Process-wide registry. Built once at startup, immutable thereafter.
#[derive(Default)]
pub struct PrimitiveRegistry {
    primitives: HashMap<&'static str, Arc<dyn ErasedPrimitive>>,
}

impl PrimitiveRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register<P: Primitive + 'static>(&mut self, p: P) {
        let name = Primitive::name(&p);
        self.primitives.insert(name, Arc::new(p));
    }

    pub fn names(&self) -> Vec<&'static str> {
        let mut names: Vec<&'static str> = self.primitives.keys().copied().collect();
        names.sort();
        names
    }

    pub fn get(&self, name: &str) -> Option<&Arc<dyn ErasedPrimitive>> {
        self.primitives.get(name)
    }

    /// Iterator over all registered primitives. The loop driver uses
    /// this to build per-session rig adapters in `loop.rs`.
    pub fn all(&self) -> Vec<Arc<dyn ErasedPrimitive>> {
        let mut names: Vec<&'static str> = self.primitives.keys().copied().collect();
        names.sort();
        names
            .into_iter()
            .filter_map(|n| self.primitives.get(n).cloned())
            .collect()
    }

    /// Tool definitions for rig's `tools` array. Each primitive's
    /// schema is pulled from `JsonSchema`; description is pulled from
    /// `Primitive::description`.
    pub fn tool_definitions(&self) -> Vec<rig::completion::ToolDefinition> {
        let mut names: Vec<&'static str> = self.primitives.keys().copied().collect();
        names.sort();
        names
            .into_iter()
            .filter_map(|name| {
                let p = self.primitives.get(name)?;
                Some(rig::completion::ToolDefinition {
                    name: name.to_string(),
                    description: p.description().to_string(),
                    parameters: p.input_schema(),
                })
            })
            .collect()
    }

    pub async fn dispatch(
        &self,
        ctx: &PrimitiveCtx<'_>,
        name: &str,
        args: Value,
    ) -> Result<DispatchOutput, PrimitiveError> {
        let p = self
            .primitives
            .get(name)
            .ok_or_else(|| PrimitiveError::InvalidInput {
                reason: format!("unknown primitive {name:?}"),
            })?;
        p.execute_erased(ctx, args).await
    }
}
