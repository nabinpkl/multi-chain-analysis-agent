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
pub use prompt::{PROMPT_V1_TAG, PROMPT_V1_TEXT, active_prompt};
pub use runtime::{Agent, build_client};
pub use stubs::{StubInfo, StubInfoWire, StubRegistry};
pub use types::{
    AgentDone, AgentRequest, AgentSessionStarted, Claim, ClaimKind, CostClass, DataSource,
    EntityRef, NodeStatsWire, NumberRef, PolicyVerdict, ProvenanceRef, StubMarker, SubgraphSlice,
    TimeScope, ViewContext,
};

/// Build the primitive registry with the ship-1 set: `wallet_profile`
/// (the only feature primitive) and `emit_claim` (claim emission
/// infrastructure that hooks the output policy).
pub fn build_registry() -> PrimitiveRegistry {
    let mut r = PrimitiveRegistry::new();
    r.register(primitives::WalletProfilePrimitive);
    r.register(primitives::EmitClaimPrimitive);
    r
}

/// Pre-register the per-primitive stubs that exist independently of
/// whether the primitive is hit. Currently: the `wallet_profile`
/// Range arm. Called once at boot so the stub banner sees the entry
/// even if nobody calls Range yet.
pub fn register_primitive_stubs(stubs: &StubRegistry) {
    stubs.register(StubInfo {
        name: "primitive.wallet_profile.range_arm",
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
}

impl AgentThread {
    pub fn new(thread_id: String, started_at_ms: u64) -> Self {
        Self {
            thread_id,
            messages: Vec::new(),
            started_at_ms,
            turn_count: 0,
        }
    }
}
