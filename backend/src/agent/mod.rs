//! Agent runtime module. Wires rig as the provider-agnostic LLM client
//! (per D-2, see `architecture-decisions/chain-analysis-agent/01-agent-overview.md`)
//! and exposes the public types/services the HTTP layer + smoke binary
//! + AppState all consume.

pub mod budget;
pub mod client;
pub mod config;
pub mod hooks;
pub mod ledger;
#[allow(clippy::module_inception)]
#[path = "loop.rs"]
pub mod loop_driver;
pub mod policy;
pub mod policy_crosscheck;
pub mod policy_prompt;
pub mod primitives;
pub mod prompt;
pub mod runtime;
pub mod stubs;
pub mod types;

pub use budget::{BudgetAxis, BudgetCheck, BudgetGate, PrincipalHash};
pub use client::AgentClient;
pub use config::AgentConfig;
pub use ledger::Ledger;
pub use policy::OutputPolicy;
pub use primitives::{PrimitiveRegistry, SseFrame};
pub use prompt::{PROMPT_V1_TAG, PROMPT_V1_TEXT, PROMPT_V2_TAG, PROMPT_V2_TEXT, active_prompt};
pub use policy_prompt::{
    POLICY_PROMPT_V1_TAG, POLICY_PROMPT_V1_TEXT, POLICY_PROMPT_V2_TAG, POLICY_PROMPT_V2_TEXT,
    POLICY_PROMPT_V3_TAG, POLICY_PROMPT_V3_TEXT, active_policy_prompt,
};
pub use runtime::{Agent, build_client};
pub use stubs::{StubInfo, StubInfoWire, StubRegistry};
pub use types::{
    AgentDone, AgentRequest, AgentSessionStarted, Claim, ClaimKind, CostClass, DataSource,
    EntityRef, NodeStatsWire, NumberRef, PolicyVerdict, ProvenanceRef, StubMarker, SubgraphSlice,
    TimeScope, ViewContext,
};

/// Build the primitive registry. Ship 3 set: `wallet_profile`,
/// `community_summary` (both real, Live-arm), and `emit_claim` (claim
/// emission infrastructure that hooks the output policy).
pub fn build_registry() -> PrimitiveRegistry {
    let mut r = PrimitiveRegistry::new();
    r.register(primitives::WalletProfilePrimitive);
    r.register(primitives::CommunitySummaryPrimitive);
    r.register(primitives::EmitClaimPrimitive);
    r
}

/// Pre-register the per-primitive stubs that exist independently of
/// whether the primitive is hit. Ship 3 adds the `community_summary`
/// Range arm alongside the existing `wallet_profile` Range arm.
/// Called once at boot so the stub banner lists them even when
/// nobody calls Range yet.
pub fn register_primitive_stubs(stubs: &StubRegistry) {
    stubs.register(StubInfo {
        name: "primitive.wallet_profile.range_arm",
        component: "primitive",
        reason: "warehouse path lands in ship 5; Live arm fully implemented",
        promoted_in_ship: 5,
    });
    stubs.register(StubInfo {
        name: "primitive.community_summary.range_arm",
        component: "primitive",
        reason: "warehouse path lands in ship 5; Live arm fully implemented",
        promoted_in_ship: 5,
    });
}

/// Pre-register thread-state stubs. `thread.in_memory_only` is hit on
/// every follow-up turn (turn >= 2 of a conversation). Surfaces the
/// fact that v1.5 thread state is in-process: no persistence, no
/// length cap, no token cap, no TTL, no per-principal scoping.
pub fn register_thread_stubs(stubs: &StubRegistry) {
    stubs.register(StubInfo {
        name: "thread.in_memory_only",
        component: "thread_state",
        reason: "threads live in-process: no persistence (refresh/restart drops), no length cap, no token cap, no TTL, no per-principal scoping. cost caps land in ship 4; persistent + recallable conversation memory is its own future phase.",
        promoted_in_ship: 4,
    });
}

// Ship 2.5 retired the `narrative.no_numerical_crosscheck` stub when
// the deterministic cross-check landed in `policy_crosscheck.rs`.
// Constitution v2's Rule 5 + the cross-check together cover what the
// stub flagged. Function deleted; the call site in `state.rs` is
// also gone. If a future audit reveals a new gap in narrative
// gating, register a new, specifically-named stub there rather than
// reviving this one.

/// In-memory thread state. v1.5: backend-owned, single source of truth
/// for the rig message vec across follow-up turns. Frontend echoes the
/// `thread_id` on every follow-up; the backend looks up here, appends,
/// stores back. Server restart clears the map (named by the
/// `thread.in_memory_only` stub).
#[derive(Debug, Clone)]
pub struct AgentThread {
    pub thread_id: String,
    pub messages: Vec<rig::message::Message>,
    pub started_at_ms: u64,
    pub turn_count: u32,
    /// Approved Claims emitted in prior turns of this thread. Ship 2.5
    /// adds this so the narrative numerical cross-check has a lenient
    /// reference set: a follow-up turn can restate a number from an
    /// earlier turn's Claim without re-fetching. Capped at
    /// `MAX_THREAD_CLAIMS` entries (FIFO drop) so memory stays
    /// bounded; the persistent-memory layer named by the
    /// `thread.in_memory_only` stub will eventually replace this.
    pub claims: Vec<crate::agent::types::Claim>,
    /// Ship 3 primitive-binding ledger. Every successful primitive
    /// dispatch in this thread gets recorded here; the policy gate's
    /// binding leg checks claim numbers + provenance refs against
    /// this store so fabricated values retract before the user sees
    /// them. Bounded by `primitives::MAX_THREAD_BINDINGS` (FIFO
    /// drop). In-memory only, same as `messages` and `claims`.
    pub bindings: primitives::PrimitiveBindingStore,
}

/// FIFO cap on `AgentThread.claims`. 20 covers ~5-10 turns of a
/// typical conversation; older Claims drop. Tunable; revisit if
/// dogfood shows long conversations losing reference numbers.
pub const MAX_THREAD_CLAIMS: usize = 20;

impl AgentThread {
    pub fn new(thread_id: String, started_at_ms: u64) -> Self {
        Self {
            thread_id,
            messages: Vec::new(),
            started_at_ms,
            turn_count: 0,
            claims: Vec::new(),
            bindings: primitives::PrimitiveBindingStore::new(),
        }
    }
}
