//! Output policy gate (phase 03 layer 3). v0 stub: always Approved.
//! Registers `policy.always_approve` in the stub registry so the UI
//! banner names it. Ship 2 swaps the body to a real cheap-model call
//! against a written constitution; the call site (in
//! `primitives/emit_claim.rs`) does not change.

use std::sync::Arc;

use super::stubs::{StubInfo, StubRegistry};
use super::types::{Claim, PolicyVerdict};

pub struct OutputPolicy {
    stubs: Arc<StubRegistry>,
}

impl OutputPolicy {
    pub fn new(stubs: Arc<StubRegistry>) -> Self {
        stubs.register(StubInfo {
            name: "policy.always_approve",
            component: "output_policy",
            reason: "real cheap-model constitution check lands in ship 2",
            promoted_in_ship: 2,
        });
        Self { stubs }
    }

    /// Per-claim verdict. v0 always Approved; ship 2 returns Retracted
    /// when the constitution rejects.
    pub async fn check(&self, _claim: &Claim) -> PolicyVerdict {
        self.stubs.hit("policy.always_approve");
        PolicyVerdict::Approved
    }
}
