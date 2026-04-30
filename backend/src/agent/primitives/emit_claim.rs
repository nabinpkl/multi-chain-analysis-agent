//! `emit_claim` primitive. The model calls this when it has a final
//! analytical statement to surface. The primitive intercepts:
//! 1. Validates the claim shape.
//! 2. Stamps `id` (ULID), `session_id`, `policy_verdict`, `stubs_active`.
//! 3. Calls `OutputPolicy::check` (stub-approves in v0).
//! 4. Writes `ClaimEmitted` + `PolicyVerdict` ledger events.
//! 5. Pushes the Claim down the SSE sink for the frontend.
//!
//! Ship 2 swaps the policy body. Ship 7 adds an `emit_pulse_claim`
//! sibling primitive following the same shape. The loop never special-
//! cases this primitive; the side effects live here.

use async_trait::async_trait;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use super::{Primitive, PrimitiveCtx, PrimitiveError, PrimitiveOutput, SseFrame};
use crate::agent::ledger::{LedgerEventDraft, LedgerEventKind};
use crate::agent::types::{
    Claim, ClaimKind, NumberRef, PolicyVerdict, ProvenanceRef, SubgraphSlice,
};

fn now_ms_since(start_ms: u64) -> u32 {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(start_ms);
    now.saturating_sub(start_ms).min(u32::MAX as u64) as u32
}

/// Input shape the model sends. Matches `Claim` minus the runtime-
/// stamped fields (`id`, `session_id`, `emitted_at_ms`,
/// `policy_verdict`, `stubs_active`, which are runtime-controlled).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EmitClaimInput {
    pub kind: ClaimKind,
    pub headline: String,
    pub body_markdown: String,
    pub provenance: Vec<ProvenanceRef>,
    #[serde(default)]
    pub support_numbers: Vec<NumberRef>,
    #[serde(default)]
    pub subgraph_slice: Option<SubgraphSlice>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
pub struct EmitClaimOutput {
    pub claim_id: String,
    pub policy: String,
}

pub struct EmitClaimPrimitive;

#[async_trait]
impl Primitive for EmitClaimPrimitive {
    type Input = EmitClaimInput;
    type Output = EmitClaimOutput;

    fn name(&self) -> &'static str {
        "emit_claim"
    }

    fn description(&self) -> &'static str {
        "\
Emit a finalized analytical claim to the user. Call this exactly once \
per session after you have gathered enough evidence via other tools. \
Provide a concise 1-line headline, a structured body_markdown (with \
${ref:N} placeholders for entity references), and a non-empty \
provenance list citing every entity backing your claim. Every claim \
MUST include at least one provenance reference; uncited claims will be \
auto-retracted by the output policy.\
"
    }

    fn data_source(&self) -> crate::agent::types::DataSource {
        crate::agent::types::DataSource::Live
    }

    fn cost_class(&self) -> crate::agent::types::CostClass {
        crate::agent::types::CostClass::Cheap
    }

    async fn execute(
        &self,
        ctx: &PrimitiveCtx<'_>,
        input: Self::Input,
    ) -> Result<PrimitiveOutput<Self::Output>, PrimitiveError> {
        // 1. Build the runtime-stamped claim.
        let claim_id = ulid::Ulid::new().to_string();
        let stubs_active = ctx.state.agent_stubs.markers_for_claim();
        let mut claim = Claim {
            id: claim_id.clone(),
            session_id: ctx.session_id.clone(),
            kind: input.kind,
            headline: input.headline,
            body_markdown: input.body_markdown,
            provenance: input.provenance,
            support_numbers: input.support_numbers,
            subgraph_slice: input.subgraph_slice,
            policy_verdict: PolicyVerdict::Approved,
            stubs_active,
            emitted_at_ms: now_ms_since(ctx.session_started_at_ms),
        };

        // 2. Output policy gate (stub returns Approved in v0).
        let verdict = ctx.state.agent_policy.check(&claim).await;
        claim.policy_verdict = verdict.clone();

        // 3. Write ledger events.
        let policy_payload = serde_json::to_string(&verdict)
            .unwrap_or_else(|_| "{}".into());
        if let Err(e) = ctx
            .state
            .agent_ledger
            .write(LedgerEventDraft {
                session_id: ctx.session_id.clone(),
                kind: LedgerEventKind::PolicyVerdict,
                principal_hash: ctx.principal_hash,
                payload: policy_payload,
                pre_estimate_units: 0,
                post_actual_units: 0,
                cost_relevant: false,
            })
            .await
        {
            warn!(error = %e, "ledger PolicyVerdict write failed");
        }
        let claim_payload = serde_json::to_string(&claim)
            .unwrap_or_else(|_| "{}".into());
        if let Err(e) = ctx
            .state
            .agent_ledger
            .write(LedgerEventDraft {
                session_id: ctx.session_id.clone(),
                kind: LedgerEventKind::ClaimEmitted,
                principal_hash: ctx.principal_hash,
                payload: claim_payload,
                pre_estimate_units: 0,
                post_actual_units: 0,
                cost_relevant: false,
            })
            .await
        {
            warn!(error = %e, "ledger ClaimEmitted write failed");
        }

        // 4. Push to SSE sink. We push regardless of verdict so the
        // frontend can render Retracted claims (greyed out) when ship
        // 2 starts producing them.
        if let Err(e) = ctx.sse.send(SseFrame::Claim(claim.clone())).await {
            warn!(error = %e, "SSE claim push failed (receiver dropped)");
        }

        let policy_str = match &verdict {
            PolicyVerdict::Approved => "approved".to_string(),
            PolicyVerdict::Retracted { reason } => format!("retracted: {reason}"),
        };
        info!(
            claim_id = %claim_id,
            kind = ?claim.kind,
            verdict = %policy_str,
            "emit_claim done"
        );

        Ok(PrimitiveOutput {
            value: EmitClaimOutput {
                claim_id,
                policy: policy_str,
            },
            // emit_claim is itself the leaf; no upstream provenance.
            provenance: vec![],
            subgraph_slice: None,
        })
    }
}
