//! Agent runtime module. Wires rig as the provider-agnostic LLM client
//! (per D-2, see `architecture-decisions/chain-analysis-agent/01-agent-overview.md`)
//! and exposes the public types/services the HTTP layer + smoke binary
//! + AppState all consume.

pub mod budget;
pub mod client;
pub mod config;
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
