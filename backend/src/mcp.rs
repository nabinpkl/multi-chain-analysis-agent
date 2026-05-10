//! MCP (Model Context Protocol) tool surface mounted as an Axum
//! `nest_service("/mcp", …)` route on the internal listener.
//!
//! Why this exists: the next architectural step after the LLM-provider
//! and per-role-elapsed plumbing is "harness engineering"  replacing
//! the in-Python Pydantic AI agent loop with a `codex exec` shell-out
//! that drives our primitives via MCP. Codex (running on GPT-5.5 via
//! the user's ChatGPT subscription) is faster than the free-tier Gemini
//! lite path AND amortizes its preamble cost across many tool calls.
//! For codex to call our primitives it needs an MCP server; this
//! module is that server.
//!
//! Why mounted on the existing internal Axum app instead of a separate
//! stdio binary: zero new state-init cost (we reuse `AppState`), zero
//! per-tool-call overhead (in-process call to the same business-logic
//! fns the existing `/primitive/*` HTTP handlers wrap), one binary,
//! one log stream, one health probe. Stdio's only architectural win is
//! "no port to manage", irrelevant on a docker-compose network where
//! port allocation is solved.
//!
//! Snapshot id passing: every read-only tool takes `snapshot_id` as a
//! JSON arg. The harness (Python loop driver, future commit) opens a
//! turn via the existing `/turn/begin` HTTP route, gets the
//! snapshot_id, embeds it in the codex developer prompt, and codex's
//! GPT-5.5 threads it through every tool call. This wastes ~30 tokens
//! per call but avoids per-session-state plumbing for v0; a future
//! iteration can move snapshot_id into MCP session state once the
//! ergonomic cost is real.
//!
//! Write-side primitive: `emit_claims` (plural, batched). Each call
//! looks up the per-snapshot mpsc sender stashed in
//! `state.claim_channels` at `turn_begin`, parses the args' claim
//! list, and pushes each claim onto the channel as a
//! `serde_json::Value`. The Python loop driver subscribes to the
//! channel via `GET /turn/{snapshot_id}/claims` (SSE drain) and
//! folds the drained list into the same gate stack today's
//! Pydantic AI `agent.tool emit_claim` flow uses. Batched (vs the
//! Python side's per-call `emit_claim`) because codex runs on
//! GPT-5.5 which handles batched structured emission well, and
//! the cost win on the harness path's whole reason-for-existing
//! (latency) scales with N chips per turn.
//!
//! Out of scope here:
//! - Codex shell-out itself, per-actor codex_home provisioning, and
//!   the constitution-gate adapter against codex prose. All separate
//!   plans.

use rmcp::handler::server::router::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use rmcp::model::CallToolResult;
use rmcp::{ErrorData as McpError, ServerHandler, schemars, tool, tool_handler, tool_router};

use crate::primitives::{community_summary, get_token_info, types::TimeScope, wallet_profile};
use crate::state::AppState;

/// Tool surface bound to one rmcp session. Holds an `AppState` clone
/// (cheap: every field is `Arc` / channel-handle internally) and the
/// generated tool router. The `StreamableHttpService` factory we pass
/// at mount time builds one of these per MCP session, so concurrent
/// codex turns can each carry their own session id without sharing
/// tool-router state.
#[derive(Clone)]
pub struct McaeMcp {
    state: AppState,
    tool_router: ToolRouter<Self>,
}

impl McaeMcp {
    pub fn new(state: AppState) -> Self {
        Self {
            state,
            tool_router: Self::tool_router(),
        }
    }
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct WalletProfileArgs {
    /// Snapshot id from the most recent `POST /turn/begin` call.
    /// The harness opens the turn, then passes this id through to
    /// every MCP tool call within the turn so reads see a consistent
    /// graph view. Snapshots expire ~5 minutes after `/turn/begin`.
    pub snapshot_id: String,
    /// Solana wallet address (base58 pubkey).
    pub addr: String,
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct CommunitySummaryArgs {
    /// Snapshot id from the most recent `POST /turn/begin` call.
    pub snapshot_id: String,
    /// Stable community label (`u32`). Source it from a prior
    /// `wallet_profile` response (the `community_id` field) or from
    /// the user's selection on the live graph.
    pub community_id: u32,
}

#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct GetTokenInfoArgs {
    /// SPL or Token-2022 mint pubkey (base58). Returns name + symbol
    /// + URI from the lazy ClickHouse-backed metadata cache, falling
    /// back to a `getAccountInfo` RPC fetch + cache write on miss.
    pub mint: String,
}

/// Args for `emit_claims`. The `claims` array is the batched chip
/// payload  codex emits all chips for a turn in one tool call,
/// rather than the per-chip pattern the Pydantic AI side uses on
/// `agent_service/agent.py::emit_claim`. The schema mirrors the
/// Python `EmitClaimInput` Pydantic model field-for-field; the
/// Python loop driver `model_validate`s each element back into
/// that model after draining, so a divergence here would surface
/// as a validation error on the gate side rather than silent loss.
#[derive(Debug, serde::Deserialize, schemars::JsonSchema)]
pub struct EmitClaimsArgs {
    /// Snapshot id from the most recent `POST /turn/begin` call.
    /// Routes the chip payloads to the per-turn channel created at
    /// `turn_begin`. Snapshot must still be open; closed / unknown
    /// returns an mcp `invalid_params` error.
    pub snapshot_id: String,
    /// One or more claims to emit. Empty list is a noop (the gate
    /// drains nothing). The Python gate stack enforces "at least
    /// one provenance ref per claim" plus value-compare against
    /// the binding store; we don't pre-validate here to avoid
    /// duplicating rules across the Rust + Python boundary.
    pub claims: Vec<ClaimInput>,
}

/// Wire shape for one claim. Field-for-field mirror of the Python
/// `agent_service.agent.EmitClaimInput` Pydantic model. Documented
/// inline so the JSON-Schema codex sees on `tools/list` carries the
/// same per-field guidance the Python tool's docstring carries.
#[derive(Debug, serde::Deserialize, serde::Serialize, schemars::JsonSchema)]
pub struct ClaimInput {
    /// Claim kind: "PROFILE" | "PATTERN" | "COMPARISON" | "SUMMARY" | "PULSE".
    pub kind: String,
    /// One sentence under 100 chars summarizing the claim.
    pub headline: String,
    /// Structured paragraph; use `${ref:N}` placeholders for chip
    /// references resolved against the `provenance` array.
    pub body_markdown: String,
    /// Non-empty list of typed entity references; `${ref:N}`
    /// resolves against this. The Python placeholder gate retracts
    /// any claim with empty provenance or unresolved `${ref:N}`.
    pub provenance: Vec<ProvenanceRefIn>,
    /// Audit-class numbers backing the claim. Optional; the
    /// structural value-compare gate matches each entry against
    /// the per-thread binding store populated from prior data-tool
    /// outputs.
    #[serde(default)]
    pub support_numbers: Vec<NumberRefIn>,
}

/// Tagged-union provenance ref. Field set varies by `kind`; we
/// keep all fields optional in one struct rather than splitting
/// into multiple types because (a) the Python side uses the same
/// shape (single struct with optional fields, parsed by `kind`
/// switch on the loop-driver side) and (b) JSON-Schema's `oneOf`
/// support across MCP clients is uneven.
#[derive(Debug, serde::Deserialize, serde::Serialize, schemars::JsonSchema)]
pub struct ProvenanceRefIn {
    /// Discriminator: "wallet" | "community" | "edge" | "time_range" | "number".
    pub kind: String,
    // wallet
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub addr: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idx: Option<i64>,
    // edge
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub edge_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub src: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dst: Option<i64>,
    // community
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<i64>,
    // time_range
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub from_s: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub to_s: Option<i64>,
    // number
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metric: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<f64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub support: Vec<String>,
}

#[derive(Debug, serde::Deserialize, serde::Serialize, schemars::JsonSchema)]
pub struct NumberRefIn {
    pub metric: String,
    pub value: f64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub support: Vec<String>,
}

#[tool_router]
impl McaeMcp {
    #[tool(
        description = "Profile a Solana wallet observed in the live snapshot. \
            Returns role, community membership, transfer counts, and top \
            counterparties. Requires snapshot_id from a prior /turn/begin call."
    )]
    async fn wallet_profile(
        &self,
        Parameters(args): Parameters<WalletProfileArgs>,
    ) -> Result<CallToolResult, McpError> {
        let snapshot = self.state.snapshot_cache.get(&args.snapshot_id).ok_or_else(|| {
            McpError::invalid_params(
                format!("snapshot {} expired or unknown; call /turn/begin first", args.snapshot_id),
                None,
            )
        })?;

        let input = wallet_profile::WalletProfileInput {
            addr: args.addr,
            time_scope: TimeScope::Live,
        };
        let out = wallet_profile::compute_with_snapshot(&self.state, &snapshot, input)
            .await
            .map_err(|e| McpError::internal_error(e.to_string(), None))?;
        // Return the inner `.value` only; the `PrimitiveOutput`
        // envelope (provenance + subgraph_slice) is internal to the
        // Python claim-chip pipeline and not relevant to codex /
        // any other MCP consumer reading tool results.
        Ok(CallToolResult::structured(
            serde_json::to_value(&out.value).unwrap_or(serde_json::Value::Null),
        ))
    }

    #[tool(
        description = "Summarize a community (cluster) in the live snapshot. \
            Returns size, internal/external volume split, edge count, and top \
            wallets. Requires snapshot_id and a stable community_id from a \
            prior wallet_profile call."
    )]
    async fn community_summary(
        &self,
        Parameters(args): Parameters<CommunitySummaryArgs>,
    ) -> Result<CallToolResult, McpError> {
        let snapshot = self.state.snapshot_cache.get(&args.snapshot_id).ok_or_else(|| {
            McpError::invalid_params(
                format!("snapshot {} expired or unknown; call /turn/begin first", args.snapshot_id),
                None,
            )
        })?;

        let input = community_summary::CommunitySummaryInput {
            community_id: args.community_id,
            time_scope: TimeScope::Live,
        };
        let out = community_summary::compute_with_snapshot(&self.state, &snapshot, input)
            .await
            .map_err(|e| McpError::internal_error(e.to_string(), None))?;
        Ok(CallToolResult::structured(
            serde_json::to_value(&out.value).unwrap_or(serde_json::Value::Null),
        ))
    }

    #[tool(
        description = "Resolve a SPL or Token-2022 mint pubkey to its name, symbol, \
            and metadata URI. Reads the lazy ClickHouse-backed metadata cache; \
            cache miss triggers a getAccountInfo RPC fetch + cache write. Does \
            NOT require snapshot_id (RPC + cache, not snapshot-bound)."
    )]
    async fn get_token_info(
        &self,
        Parameters(args): Parameters<GetTokenInfoArgs>,
    ) -> Result<CallToolResult, McpError> {
        let out = get_token_info::compute(&self.state, &args.mint)
            .await
            .map_err(|e| McpError::internal_error(e.to_string(), None))?;
        Ok(CallToolResult::structured(serde_json::to_value(&out).unwrap_or(serde_json::Value::Null)))
    }

    #[tool(
        description = "Emit one or more analytical claims to the user. Call \
            after gathering enough evidence via the read-only tools. Each \
            claim must include a non-empty provenance list and \
            ${ref:N} placeholders in body_markdown that resolve against \
            it; uncited claims are auto-retracted by the harness's gate \
            stack. Batched: emit ALL chips for the turn in one call."
    )]
    async fn emit_claims(
        &self,
        Parameters(args): Parameters<EmitClaimsArgs>,
    ) -> Result<CallToolResult, McpError> {
        // Look up the per-snapshot sender stashed at turn_begin.
        // Missing => snapshot was never opened, already ended, or
        // expired. Same error shape as the read tools' snapshot
        // lookup so codex sees one canonical "snapshot's gone" hint.
        let tx = self
            .state
            .claim_channels
            .get(&args.snapshot_id)
            .ok_or_else(|| {
                McpError::invalid_params(
                    format!(
                        "snapshot {} not open for claim emission; call /turn/begin first",
                        args.snapshot_id
                    ),
                    None,
                )
            })?
            .clone();

        let mut accepted: Vec<String> = Vec::with_capacity(args.claims.len());
        for claim in args.claims.iter() {
            let value = serde_json::to_value(claim)
                .map_err(|e| McpError::internal_error(e.to_string(), None))?;
            tx.send(value).map_err(|_| {
                // Channel closed mid-call: receiver was dropped
                // (turn_end fired between snapshot check and send).
                // Race window is narrow but real; return a clear
                // signal so codex can decide to retry or move on.
                McpError::internal_error(
                    "claim channel closed; turn_end fired during emit_claims".to_string(),
                    None,
                )
            })?;
            // Synthetic per-claim id so codex can refer back to it
            // in subsequent narrative if it wants. Real claim id is
            // assigned by the loop driver after the gate stack runs.
            // Reusing the `ulid` dep already in scope (snapshot ids
            // use the same generator) so we don't pull a new uuid
            // crate just for this one ack-shape niceness.
            accepted.push(format!("draft:{}", ulid::Ulid::new()));
        }

        let ack = serde_json::json!({
            "accepted": accepted.len(),
            "draft_ids": accepted,
        });
        Ok(CallToolResult::structured(ack))
    }
}

#[tool_handler]
impl ServerHandler for McaeMcp {}
