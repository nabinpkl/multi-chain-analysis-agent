//! Cost-rate-limit gate (phase 05). v0 stub: always Ok. Registers
//! `budget.always_allow` in the stub registry so the UI banner names
//! it. Ship 4 swaps the body to multi-axis token buckets; the call
//! sites (in the loop and primitives) do not change.

use std::sync::Arc;

use super::stubs::{StubInfo, StubRegistry};
use super::types::CostClass;

/// Principal hash placeholder. v0 is a zero array; ship 4 fills it
/// from `sha256(session_cookie || truncated_ip)` per phase 05.
pub type PrincipalHash = [u8; 32];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BudgetAxis {
    /// LLM tokens consumed per pre-flight reservation + post-actual
    /// settle. v0: zero on both.
    Tokens,
    /// ClickHouse / live-graph time in milliseconds.
    DbTimeMs,
    /// Tool calls per session.
    ToolCalls,
    /// Concurrent open sessions per principal.
    Sessions,
}

#[derive(Debug, Clone)]
pub enum BudgetCheck {
    Ok,
    Denied {
        reason: String,
        retry_after_ms: u32,
    },
}

pub struct BudgetGate {
    stubs: Arc<StubRegistry>,
}

impl BudgetGate {
    pub fn new(stubs: Arc<StubRegistry>) -> Self {
        stubs.register(StubInfo {
            name: "budget.always_allow",
            component: "cost_gate",
            reason: "principal hashing + multi-axis buckets + pre-flight land in ship 4",
            promoted_in_ship: 4,
        });
        Self { stubs }
    }

    /// Pre-flight reservation. v0 always returns Ok and increments the
    /// stub hit counter. Ship 4 returns `Denied` when the principal's
    /// bucket lacks capacity.
    pub fn check_pre(
        &self,
        _principal: &PrincipalHash,
        _cost_class: CostClass,
        _axis: BudgetAxis,
        _est_units: u32,
    ) -> BudgetCheck {
        self.stubs.hit("budget.always_allow");
        BudgetCheck::Ok
    }

    /// Post-actual settle. v0 records nothing because no buckets exist
    /// yet; ship 4 decrements the principal's bucket by `actual_units`
    /// and writes drift telemetry to the ledger.
    pub fn record_post(
        &self,
        _principal: &PrincipalHash,
        _axis: BudgetAxis,
        _actual_units: u32,
    ) {
        // intentionally no-op in v0
    }
}
