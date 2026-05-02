//! Output-policy gate (phase 03 layer 3). Ship 2 promoted this from
//! the always-approve stub to a real cheap-model call against the
//! constitution. Ship 2.7 split narrative gating into multiple
//! independent legs. Ship 3 added the binding leg. Ship 3.5
//! reorganizes the legs around three concrete behavior **switches**:
//!
//! - `stay_in_role`: identity, scope, conduct (constitution leg).
//! - `dont_fabricate`: claim numbers + entities trace to real tool
//!   output (binding leg, narrative + claim).
//! - `cross_check`: prose-vs-claim consistency, with three
//!   sub-modes:
//!   - `text_match`: regex extractor (deterministic, brittle on
//!     paraphrase, recall-based).
//!   - `paraphrase_aware_match`: LLM extractor (paraphrase-robust,
//!     recall-based).
//!   - `ground_truth_match`: re-query the database / warehouse
//!     against the prose. **Stub for ship 3.5**; real implementation
//!     lands in ship 5 with warehouse primitives. Today, flipping
//!     this switch on yields a `NotApplicable { detail: "not
//!     implemented yet (lands in ship 5)" }` path step. Toggle is
//!     present so the panel shape is stable across the ship 3.5 →
//!     ship 5 transition.
//!
//! Each leg's switch is checked first; off-switch legs return
//! `SubVerdict::NotApplicable { detail: "switch off" }` and contribute
//! neither approve nor retract to the merge. Show-all-default-strict
//! merge: any retract → wire retract; the per-leg breakdown +
//! executed path are surfaced via the `GatePath` SSE frame when the
//! request's `show_trace=true`.

use std::time::Instant;

use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use super::client::AgentClient;
use super::policy_crosscheck::{
    cross_check_extracted_pair, CrosscheckConfig, ExtractedNumber, LlmExtractedNumber,
};
use super::policy_placeholder::validate_refs;
use super::policy_prompt::{POLICY_PROMPT_V4_TAG, POLICY_PROMPT_V4_TEXT};
use super::policy_structural::verify_chip_values;
use super::primitives::PrimitiveBindingStore;
use super::types::{AgentSwitches, Claim, GatePath, PathState, PathStep, PolicyVerdict, ProvenanceRef};

/// Per-channel context the gate sees. Used to build the user-message
/// JSON sent to the cheap model.
#[derive(serde::Serialize)]
struct GateRequest<'a, T: serde::Serialize> {
    channel: &'static str,
    payload: &'a T,
    #[serde(skip_serializing_if = "<[Claim]>::is_empty")]
    same_turn_claims: &'a [Claim],
}

/// Constitution v3 response shape. `extraction` is optional so a
/// malformed or older-style v1/v2 response still parses cleanly.
#[derive(Deserialize)]
struct GateResponse {
    verdict: String,
    #[serde(default)]
    reason: String,
    #[serde(default)]
    extraction: Option<LlmExtraction>,
}

#[derive(Deserialize, Debug, Clone, Serialize)]
pub struct LlmExtraction {
    #[serde(default)]
    pub narrative_numbers: Vec<LlmExtractedNumber>,
    #[serde(default)]
    pub claim_numbers: Vec<LlmExtractedNumber>,
}

/// One leg's verdict. `NotApplicable` covers both "switch off" and
/// "couldn't run" (malformed extraction, parse failure). Both
/// contribute neither approve nor retract to the merge; the other
/// legs decide.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum SubVerdict {
    Approved,
    Retracted { reason: String },
    NotApplicable { detail: String },
}

impl SubVerdict {
    fn label(&self) -> &'static str {
        match self {
            SubVerdict::Approved => "approved",
            SubVerdict::Retracted { .. } => "retracted",
            SubVerdict::NotApplicable { .. } => "n/a",
        }
    }

    fn to_path_state(&self) -> PathState {
        match self {
            SubVerdict::Approved => PathState::Approved,
            SubVerdict::Retracted { reason } => PathState::Retracted {
                reason: reason.clone(),
            },
            SubVerdict::NotApplicable { detail } => PathState::NotApplicable {
                detail: detail.clone(),
            },
        }
    }
}

/// Narrative gate breakdown (ship 3.5). One field per behavior
/// switch + nested cross-check breakdown. Fields named after the
/// switches (not the implementation legs) so the wire shape stays
/// durable as future ships strengthen behaviors.
#[derive(Debug, Clone, Serialize)]
pub struct NarrativeBreakdown {
    pub stay_in_role: SubVerdict,
    pub dont_fabricate: SubVerdict,
    pub cross_check: CrossCheckBreakdown,
}

#[derive(Debug, Clone, Serialize)]
pub struct CrossCheckBreakdown {
    pub paraphrase_aware_match: SubVerdict,
    pub ground_truth_match: SubVerdict,
}

/// Claim gate breakdown. Two switches relevant to claims:
/// `stay_in_role` (constitution) and `dont_fabricate` (binding).
/// Cross-check sub-modes don't apply to claims (claim IS the
/// structured side of the prose-vs-claim relationship).
#[derive(Debug, Clone, Serialize)]
pub struct ClaimBreakdown {
    pub stay_in_role: SubVerdict,
    pub dont_fabricate: SubVerdict,
}

/// Result of a narrative gate run. Loop reads `verdict` for retry /
/// SSE control, hands `breakdown` to the ledger, ships `path` as
/// `SseFrame::GatePath` when `show_trace=true`.
pub struct NarrativeGateResult {
    pub verdict: PolicyVerdict,
    pub breakdown: NarrativeBreakdown,
    pub path: GatePath,
    pub raw_extraction: Option<LlmExtraction>,
}

/// Result of a claim gate run.
pub struct ClaimGateResult {
    pub verdict: PolicyVerdict,
    pub breakdown: ClaimBreakdown,
    pub path: GatePath,
}

// ============================================================================
// PathBuilder
// ============================================================================

/// Accumulates `PathStep`s during a gate run. Caps at
/// `MAX_PATH_STEPS` to bound runaway. Every leg (or skip) appends
/// exactly one step.
const MAX_PATH_STEPS: usize = 32;

struct PathBuilder {
    channel: String,
    switches: AgentSwitches,
    steps: Vec<PathStep>,
    started_at: Instant,
}

impl PathBuilder {
    fn new(channel: &str, switches: &AgentSwitches) -> Self {
        Self {
            channel: channel.to_string(),
            switches: switches.clone(),
            steps: Vec::new(),
            started_at: Instant::now(),
        }
    }

    /// Record a step that ran. `note` is a human-readable summary
    /// of what was checked.
    fn record(&mut self, stage: &str, sub: &SubVerdict, note: impl Into<String>) {
        if self.steps.len() >= MAX_PATH_STEPS {
            return;
        }
        let elapsed_us = self.elapsed_us();
        self.steps.push(PathStep {
            stage: stage.to_string(),
            state: sub.to_path_state(),
            elapsed_us,
            note: note.into(),
        });
    }

    /// Record a skip (switch off, no leg ran).
    fn skip(&mut self, stage: &str, detail: &str) {
        if self.steps.len() >= MAX_PATH_STEPS {
            return;
        }
        let elapsed_us = self.elapsed_us();
        self.steps.push(PathStep {
            stage: stage.to_string(),
            state: PathState::NotApplicable {
                detail: detail.to_string(),
            },
            elapsed_us,
            note: format!("skipped ({detail})"),
        });
    }

    fn elapsed_us(&self) -> u32 {
        self.started_at
            .elapsed()
            .as_micros()
            .min(u32::MAX as u128) as u32
    }

    fn finish(self, final_verdict: PolicyVerdict) -> GatePath {
        GatePath {
            channel: self.channel,
            switches: self.switches,
            steps: self.steps,
            final_verdict,
        }
    }
}

/// Tiny helper: run `f` only if the switch is on; otherwise record
/// a skip and return a `NotApplicable` SubVerdict. Used everywhere
/// a switch gates a leg.
fn guarded<F: FnOnce() -> (SubVerdict, String)>(
    on: bool,
    path: &mut PathBuilder,
    stage: &str,
    f: F,
) -> SubVerdict {
    if on {
        let (sub, note) = f();
        path.record(stage, &sub, note);
        sub
    } else {
        path.skip(stage, "switch off");
        SubVerdict::NotApplicable {
            detail: "switch off".into(),
        }
    }
}

// ============================================================================
// OutputPolicy
// ============================================================================

/// Output-policy gate. Owns a clone of the agent client so the
/// `policy_model` call site is one place.
pub struct OutputPolicy {
    client: Option<AgentClient>,
    constitution_tag: &'static str,
    constitution_text: &'static str,
}

impl OutputPolicy {
    pub fn new(client: Option<AgentClient>) -> Self {
        match client.as_ref() {
            Some(c) => info!(
                tag = POLICY_PROMPT_V4_TAG,
                len = POLICY_PROMPT_V4_TEXT.len(),
                policy_model = c.policy_model(),
                "policy gate online (constitution v4, ship 5a citation discipline)",
            ),
            None => warn!(
                "policy gate constructed without an agent client; \
                 gate methods will auto-approve. agent endpoints \
                 should 503 in this configuration so the auto-approve \
                 path is unreachable in practice.",
            ),
        }
        Self {
            client,
            constitution_tag: POLICY_PROMPT_V4_TAG,
            constitution_text: POLICY_PROMPT_V4_TEXT,
        }
    }

    pub fn constitution_tag(&self) -> &'static str {
        self.constitution_tag
    }

    /// Verdict for a Claim about to be emitted (ship 3.5 switch-aware).
    /// Two switches relevant to the claim channel:
    /// - `stay_in_role`: constitution leg.
    /// - `dont_fabricate`: binding leg.
    ///
    /// Both off => no LLM call, claim flows through with merged
    /// `Approved` (NotApplicable from both legs counts as no-flag).
    pub async fn check_claim(
        &self,
        claim: &Claim,
        binding_store: &PrimitiveBindingStore,
        switches: &AgentSwitches,
    ) -> ClaimGateResult {
        let mut path = PathBuilder::new("claim", switches);

        // stay_in_role: realized today by the constitution leg. The
        // LLM call only runs when the switch is on; raw-LLM preset
        // pays zero gate-side latency for claims.
        let stay_in_role_v = if switches.stay_in_role {
            let (sub, note) = self.run_claim_constitution(claim).await;
            path.record("claim.stay_in_role", &sub, note);
            sub
        } else {
            path.skip("claim.stay_in_role", "switch off");
            SubVerdict::NotApplicable {
                detail: "switch off".into(),
            }
        };

        // don't fabricate: ship 5a's two-stage structural check.
        // Stage 1: every `${ref:N}` in claim.body_markdown resolves
        //   to claim.provenance (placeholder validator).
        // Stage 2: every Number ref's value traces to the binding
        //   store; entity refs trace to all_wallets / all_communities
        //   (structural compare).
        // The breakdown's `dont_fabricate` SubVerdict is the AND of
        // the two stages (retract on either).
        let dont_fabricate_v = if switches.dont_fabricate {
            let placeholder_sub = run_placeholder_leg_claim(claim);
            let placeholder_note = match &placeholder_sub {
                SubVerdict::Approved => format!(
                    "all chip indices in claim resolve to {} provenance entries",
                    claim.provenance.len(),
                ),
                SubVerdict::Retracted { reason } => format!("placeholder: {reason}"),
                SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
            };
            path.record(
                "claim.placeholder_validation",
                &placeholder_sub,
                placeholder_note,
            );

            if matches!(placeholder_sub, SubVerdict::Retracted { .. }) {
                path.skip(
                    "claim.structural_value_compare",
                    "skipped after placeholder failure",
                );
                placeholder_sub
            } else {
                let structural_sub =
                    run_structural_leg(&claim.provenance, binding_store);
                let structural_note = match &structural_sub {
                    SubVerdict::Approved => format!(
                        "all chip values traced to {} primitive output(s)",
                        binding_store.len(),
                    ),
                    SubVerdict::Retracted { reason } => format!("structural: {reason}"),
                    SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
                };
                path.record(
                    "claim.structural_value_compare",
                    &structural_sub,
                    structural_note,
                );
                structural_sub
            }
        } else {
            path.skip("claim.placeholder_validation", "switch off");
            path.skip("claim.structural_value_compare", "switch off");
            SubVerdict::NotApplicable {
                detail: "switch off".into(),
            }
        };

        let breakdown = ClaimBreakdown {
            stay_in_role: stay_in_role_v,
            dont_fabricate: dont_fabricate_v,
        };

        info!(
            target: "agent::policy::claim",
            stay_in_role = %breakdown.stay_in_role.label(),
            dont_fabricate = %breakdown.dont_fabricate.label(),
            claim_id = %claim.id,
            "claim gate merged",
        );

        let verdict = merge_claim(&breakdown);
        ClaimGateResult {
            verdict: verdict.clone(),
            breakdown,
            path: path.finish(verdict),
        }
    }

    async fn run_claim_constitution(&self, claim: &Claim) -> (SubVerdict, String) {
        let req = GateRequest {
            channel: "claim",
            payload: claim,
            same_turn_claims: &[],
        };
        let user = match serde_json::to_string(&req) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = %e, "policy: serialize claim payload failed");
                let sub = SubVerdict::Retracted {
                    reason: "policy serialize failure".into(),
                };
                let note = "constitution: serialize failure".to_string();
                return (sub, note);
            }
        };
        let (sub, _extraction) = self.run_gate("claim", &user).await;
        let note = match &sub {
            SubVerdict::Approved => "constitution rules 1-6 approved".to_string(),
            SubVerdict::Retracted { reason } => format!("constitution: {reason}"),
            SubVerdict::NotApplicable { detail } => format!("constitution n/a: {detail}"),
        };
        (sub, note)
    }

    /// Switch-aware narrative gate (ship 3.5). Three behavior
    /// switches; cross-check has three sub-modes. Off-switch legs
    /// short-circuit to `NotApplicable { "switch off" }` and the
    /// LLM call is skipped entirely when no LLM-dependent switch
    /// is on (raw-LLM preset has zero gate overhead).
    pub async fn check_narrative(
        &self,
        text: &str,
        same_turn_claims: &[Claim],
        thread_history_claims: &[Claim],
        binding_store: &PrimitiveBindingStore,
        switches: &AgentSwitches,
    ) -> NarrativeGateResult {
        let mut path = PathBuilder::new("narrative", switches);

        // Ship 5a: assemble narrative provenance from this turn's
        // claims (in emission order). The model's `${ref:N}` indices
        // resolve against this flat vec; placeholder validation +
        // structural compare both walk it. Same assembly the loop
        // does for the SSE emit (kept as a separate helper there
        // because the loop assembles after gate approval).
        let narrative_provenance: Vec<ProvenanceRef> = same_turn_claims
            .iter()
            .flat_map(|c| c.provenance.iter().cloned())
            .collect();

        // The LLM call backs both `stay_in_role` (constitution
        // verdict) and `cross_check.paraphrase_aware_match`
        // (extraction sidecar). Skip the call entirely when neither
        // is on; "raw LLM" preset pays zero gate-side latency.
        let need_llm = needs_llm_call(switches);
        let (constitution_sub, raw_extraction) = if need_llm {
            let payload = serde_json::json!({ "text": text });
            let req = GateRequest {
                channel: "narrative",
                payload: &payload,
                same_turn_claims,
            };
            match serde_json::to_string(&req) {
                Ok(user) => self.run_gate("narrative", &user).await,
                Err(e) => {
                    warn!(error = %e, "policy: serialize narrative payload failed");
                    (
                        SubVerdict::Retracted {
                            reason: "policy serialize failure".into(),
                        },
                        None,
                    )
                }
            }
        } else {
            (
                SubVerdict::NotApplicable {
                    detail: "switch off".into(),
                },
                None,
            )
        };

        // stay_in_role: consume the constitution verdict.
        let stay_in_role_v = if switches.stay_in_role {
            let note = match &constitution_sub {
                SubVerdict::Approved => "constitution rules approved".to_string(),
                SubVerdict::Retracted { reason } => format!("constitution: {reason}"),
                SubVerdict::NotApplicable { detail } => format!("constitution n/a: {detail}"),
            };
            path.record("narrative.stay_in_role", &constitution_sub, note);
            constitution_sub.clone()
        } else {
            path.skip("narrative.stay_in_role", "switch off");
            SubVerdict::NotApplicable {
                detail: "switch off".into(),
            }
        };

        // don't fabricate: ship 5a's two-stage structural check.
        // Stage 1: every `${ref:N}` in narrative resolves to the
        //   assembled narrative_provenance vec (placeholder validator).
        // Stage 2: every Number ref's value traces to the binding
        //   store; entity refs trace to all_wallets / all_communities
        //   (structural compare).
        // The breakdown's `dont_fabricate` SubVerdict is the AND of
        // the two stages (retract on either).
        let dont_fabricate_v = if switches.dont_fabricate {
            let placeholder_sub =
                run_placeholder_leg_narrative(text, &narrative_provenance);
            let placeholder_note = match &placeholder_sub {
                SubVerdict::Approved => format!(
                    "all chip indices in narrative resolve to {} provenance entries",
                    narrative_provenance.len(),
                ),
                SubVerdict::Retracted { reason } => format!("placeholder: {reason}"),
                SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
            };
            path.record(
                "narrative.placeholder_validation",
                &placeholder_sub,
                placeholder_note,
            );

            if matches!(placeholder_sub, SubVerdict::Retracted { .. }) {
                // Skip structural compare when placeholder failed;
                // value lookup against an unresolved ref is
                // meaningless. Surface a path step so the trace
                // shows the cascade.
                path.skip(
                    "narrative.structural_value_compare",
                    "skipped after placeholder failure",
                );
                placeholder_sub
            } else {
                let structural_sub =
                    run_structural_leg(&narrative_provenance, binding_store);
                let structural_note = match &structural_sub {
                    SubVerdict::Approved => format!(
                        "all chip values traced to {} primitive output(s)",
                        binding_store.len(),
                    ),
                    SubVerdict::Retracted { reason } => format!("structural: {reason}"),
                    SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
                };
                path.record(
                    "narrative.structural_value_compare",
                    &structural_sub,
                    structural_note,
                );
                structural_sub
            }
        } else {
            path.skip("narrative.placeholder_validation", "switch off");
            path.skip("narrative.structural_value_compare", "switch off");
            SubVerdict::NotApplicable {
                detail: "switch off".into(),
            }
        };

        // cross_check: paraphrase + ground-truth (text_match retired
        // in ship 5a). Both advisory in the merge below.
        let cc = &switches.cross_check;

        let paraphrase_v = guarded(
            cc.paraphrase_aware_match,
            &mut path,
            "narrative.cross_check.paraphrase_aware_match",
            || {
                let sub = match raw_extraction.as_ref() {
                    Some(extraction) => {
                        let binding_numbers = binding_store.all_numbers();
                        let narr: Vec<ExtractedNumber> = extraction
                            .narrative_numbers
                            .iter()
                            .map(|n| n.into_extracted())
                            .collect();
                        let claims: Vec<ExtractedNumber> = extraction
                            .claim_numbers
                            .iter()
                            .map(|n| n.into_extracted())
                            .collect();
                        match cross_check_extracted_pair(
                            &narr,
                            &claims,
                            &binding_numbers,
                            CrosscheckConfig::default(),
                        ) {
                            Ok(()) => SubVerdict::Approved,
                            Err(reason) => SubVerdict::Retracted {
                                reason: reason.to_human_string(),
                            },
                        }
                    }
                    None => SubVerdict::NotApplicable {
                        detail: "extraction missing or malformed".into(),
                    },
                };
                let note = match &sub {
                    SubVerdict::Approved => "LLM extractor: prose coherent with cited claim values".to_string(),
                    SubVerdict::Retracted { reason } => reason.clone(),
                    SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
                };
                (sub, note)
            },
        );

        // ground_truth_match: stub for ship 5a; ship 5b replaces
        // this body with a real warehouse re-query.
        let ground_truth_v = if cc.ground_truth_match {
            let sub = SubVerdict::NotApplicable {
                detail: "not implemented yet (lands in ship 5b: warehouse primitives)".into(),
            };
            path.record(
                "narrative.cross_check.ground_truth_match",
                &sub,
                "stub for ship 5a; ship 5b wires the warehouse re-query path",
            );
            sub
        } else {
            path.skip("narrative.cross_check.ground_truth_match", "switch off");
            SubVerdict::NotApplicable {
                detail: "switch off".into(),
            }
        };

        let breakdown = NarrativeBreakdown {
            stay_in_role: stay_in_role_v,
            dont_fabricate: dont_fabricate_v,
            cross_check: CrossCheckBreakdown {
                paraphrase_aware_match: paraphrase_v,
                ground_truth_match: ground_truth_v,
            },
        };
        let verdict = merge_narrative(&breakdown);

        info!(
            target: "agent::policy::narrative",
            stay_in_role = %breakdown.stay_in_role.label(),
            dont_fabricate = %breakdown.dont_fabricate.label(),
            cross_paraphrase = %breakdown.cross_check.paraphrase_aware_match.label(),
            cross_truth = %breakdown.cross_check.ground_truth_match.label(),
            same_turn = same_turn_claims.len(),
            thread_history = thread_history_claims.len(),
            bindings = binding_store.len(),
            narrative_provenance_len = narrative_provenance.len(),
            "narrative gate merged",
        );

        // thread_history_claims kept in the signature for future
        // ships but currently unused by the structural path (refs
        // resolve only against this turn's accumulated claims).
        let _ = thread_history_claims;

        NarrativeGateResult {
            verdict: verdict.clone(),
            breakdown,
            path: path.finish(verdict),
            raw_extraction,
        }
    }

    async fn run_gate(
        &self,
        channel: &str,
        user: &str,
    ) -> (SubVerdict, Option<LlmExtraction>) {
        let client = match &self.client {
            Some(c) => c,
            None => return (SubVerdict::Approved, None),
        };
        let raw = match client
            .complete_policy(self.constitution_text, user)
            .await
        {
            Ok(s) => s,
            Err(e) => {
                warn!(channel, error = %e, "policy: cheap-model call failed");
                return (
                    SubVerdict::Retracted {
                        reason: "policy gate unavailable".into(),
                    },
                    None,
                );
            }
        };

        let parsed: Option<GateResponse> = serde_json::from_str(raw.trim()).ok().or_else(|| {
            extract_json_object(&raw).and_then(|slice| serde_json::from_str(slice).ok())
        });

        let resp = match parsed {
            Some(r) => r,
            None => {
                warn!(
                    channel,
                    raw = %truncate_for_log(&raw),
                    "policy: response did not parse as v3 JSON",
                );
                return (
                    SubVerdict::Retracted {
                        reason: "policy parse failure".into(),
                    },
                    None,
                );
            }
        };

        let constitution = match resp.verdict.trim().to_ascii_lowercase().as_str() {
            "approve" | "approved" => SubVerdict::Approved,
            "retract" | "retracted" | "reject" | "rejected" => {
                let reason = if resp.reason.trim().is_empty() {
                    "constitution violation".to_string()
                } else {
                    resp.reason
                };
                SubVerdict::Retracted { reason }
            }
            other => {
                warn!(
                    channel,
                    verdict = %other,
                    "policy: unknown verdict; failing closed",
                );
                SubVerdict::Retracted {
                    reason: "policy unknown verdict".into(),
                }
            }
        };

        (constitution, resp.extraction)
    }
}

// ============================================================================
// LLM call short-circuit
// ============================================================================

/// True iff the cheap-model gate call is needed for any switch on
/// this turn. The LLM response backs `stay_in_role` (constitution
/// verdict) and `cross_check.paraphrase_aware_match` (extraction
/// sidecar). When both are off, skip the call.
fn needs_llm_call(s: &AgentSwitches) -> bool {
    s.stay_in_role || s.cross_check.paraphrase_aware_match
}

// ============================================================================
// Ship 5a leg helpers (placeholder validation + structural compare)
// ============================================================================
//
// These replaced ship 3's regex-on-prose binding leg. Both legs run
// only typed lookups: placeholder validation walks `${ref:N}` tokens
// and checks each index in bounds; structural compare walks the
// provenance vec and checks each entry against the binding store.
// No prose interpretation, no unicode hazard, no unit classification
// from text.

fn run_placeholder_leg_claim(claim: &Claim) -> SubVerdict {
    // Validate refs in BOTH headline and body_markdown. The model
    // is permitted to use chips in either (today the prompt only
    // mentions body_markdown but headline citations are reasonable
    // and we'd rather reject inconsistent indices than silently
    // fail a future feature).
    let mut combined =
        String::with_capacity(claim.headline.len() + claim.body_markdown.len() + 1);
    combined.push_str(&claim.headline);
    combined.push('\n');
    combined.push_str(&claim.body_markdown);
    match validate_refs(&combined, claim.provenance.len()) {
        Ok(()) => SubVerdict::Approved,
        Err(e) => SubVerdict::Retracted {
            reason: e.to_human_string(),
        },
    }
}

fn run_placeholder_leg_narrative(
    text: &str,
    narrative_provenance: &[ProvenanceRef],
) -> SubVerdict {
    match validate_refs(text, narrative_provenance.len()) {
        Ok(()) => SubVerdict::Approved,
        Err(e) => SubVerdict::Retracted {
            reason: e.to_human_string(),
        },
    }
}

fn run_structural_leg(
    provenance: &[ProvenanceRef],
    store: &PrimitiveBindingStore,
) -> SubVerdict {
    match verify_chip_values(provenance, store) {
        Ok(()) => SubVerdict::Approved,
        Err(e) => SubVerdict::Retracted {
            reason: e.to_human_string(),
        },
    }
}

// ============================================================================
// Merges
// ============================================================================

fn merge_narrative(b: &NarrativeBreakdown) -> PolicyVerdict {
    // Ship 5a strict merge: stay_in_role + dont_fabricate drive
    // wire verdict. cross_check.paraphrase_aware_match is advisory
    // (recorded in breakdown + path trace, but does not retract by
    // itself; the structural compare in dont_fabricate carries the
    // factuality role). cross_check.ground_truth_match is the ship
    // 5b stub and never returns Retracted.
    let mut reasons: Vec<String> = Vec::new();
    push_if_retract(&b.stay_in_role, "stay-in-role", &mut reasons);
    push_if_retract(&b.dont_fabricate, "don't-fabricate", &mut reasons);
    if reasons.is_empty() {
        PolicyVerdict::Approved
    } else {
        PolicyVerdict::Retracted {
            reason: reasons.join("; "),
        }
    }
}

fn merge_claim(b: &ClaimBreakdown) -> PolicyVerdict {
    let mut reasons: Vec<String> = Vec::new();
    push_if_retract(&b.stay_in_role, "stay-in-role", &mut reasons);
    push_if_retract(&b.dont_fabricate, "don't-fabricate", &mut reasons);
    if reasons.is_empty() {
        PolicyVerdict::Approved
    } else {
        PolicyVerdict::Retracted {
            reason: reasons.join("; "),
        }
    }
}

fn push_if_retract(v: &SubVerdict, label: &str, out: &mut Vec<String>) {
    if let SubVerdict::Retracted { reason } = v {
        out.push(format!("{label}: {reason}"));
    }
}

// ============================================================================
// JSON helpers
// ============================================================================

fn extract_json_object(s: &str) -> Option<&str> {
    let start = s.find('{')?;
    let bytes = s.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut escape = false;
    for (i, &b) in bytes.iter().enumerate().skip(start) {
        if in_string {
            if escape {
                escape = false;
            } else if b == b'\\' {
                escape = true;
            } else if b == b'"' {
                in_string = false;
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&s[start..=i]);
                }
            }
            _ => {}
        }
    }
    None
}

fn truncate_for_log(s: &str) -> String {
    const MAX: usize = 240;
    if s.len() <= MAX {
        s.to_string()
    } else {
        let mut out = s[..MAX].to_string();
        out.push_str("…");
        out
    }
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::primitives::{build_binding, PrimitiveBindingStore};
    use crate::agent::types::{ClaimKind, CrossCheckSwitches, NumberRef};
    use serde_json::json;

    fn all_on_switches() -> AgentSwitches {
        AgentSwitches {
            stay_in_role: true,
            dont_fabricate: true,
            cross_check: CrossCheckSwitches {
                paraphrase_aware_match: true,
                ground_truth_match: false,
            },
            dont_repeat_yourself: true,
        }
    }

    fn all_off_switches() -> AgentSwitches {
        AgentSwitches {
            stay_in_role: false,
            dont_fabricate: false,
            cross_check: CrossCheckSwitches {
                paraphrase_aware_match: false,
                ground_truth_match: false,
            },
            dont_repeat_yourself: false,
        }
    }

    fn empty_narrative_breakdown() -> NarrativeBreakdown {
        NarrativeBreakdown {
            stay_in_role: SubVerdict::Approved,
            dont_fabricate: SubVerdict::Approved,
            cross_check: CrossCheckBreakdown {
                paraphrase_aware_match: SubVerdict::Approved,
                ground_truth_match: SubVerdict::NotApplicable {
                    detail: "switch off".into(),
                },
            },
        }
    }

    fn mk_claim(headline: &str, body: &str, provenance: Vec<ProvenanceRef>) -> Claim {
        Claim {
            id: "test".into(),
            session_id: "sess".into(),
            kind: ClaimKind::Profile,
            headline: headline.into(),
            body_markdown: body.into(),
            provenance,
            support_numbers: Vec::<NumberRef>::new(),
            subgraph_slice: None,
            policy_verdict: PolicyVerdict::Approved,
            stubs_active: vec![],
            emitted_at_ms: 0,
        }
    }

    fn store_with_one_wallet_profile() -> PrimitiveBindingStore {
        let mut store = PrimitiveBindingStore::new();
        let provenance = vec![
            ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: Some(0),
            },
            ProvenanceRef::Community { id: 42 },
        ];
        store.record(build_binding(
            "wallet_profile",
            "wp:1".into(),
            1,
            &json!({ "degree": 33, "volume": 12.4, "community_id": 42 }),
            &provenance,
        ));
        store
    }

    // --- merge --------------------------------------------------------------

    #[test]
    fn merge_narrative_all_approved() {
        assert!(matches!(
            merge_narrative(&empty_narrative_breakdown()),
            PolicyVerdict::Approved
        ));
    }

    #[test]
    fn merge_narrative_one_retract_propagates() {
        let mut b = empty_narrative_breakdown();
        b.stay_in_role = SubVerdict::Retracted {
            reason: "rule 1 (domain) violated".into(),
        };
        match merge_narrative(&b) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("stay-in-role: rule 1"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_narrative_n_a_does_not_block() {
        let mut b = empty_narrative_breakdown();
        b.cross_check.paraphrase_aware_match = SubVerdict::NotApplicable {
            detail: "switch off".into(),
        };
        assert!(matches!(merge_narrative(&b), PolicyVerdict::Approved));
    }

    #[test]
    fn merge_narrative_multiple_retracts_concatenated() {
        let b = NarrativeBreakdown {
            stay_in_role: SubVerdict::Retracted { reason: "X".into() },
            dont_fabricate: SubVerdict::Retracted { reason: "Y".into() },
            cross_check: CrossCheckBreakdown {
                paraphrase_aware_match: SubVerdict::Approved,
                ground_truth_match: SubVerdict::NotApplicable {
                    detail: "switch off".into(),
                },
            },
        };
        match merge_narrative(&b) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("stay-in-role: X"));
                assert!(reason.contains("don't-fabricate: Y"));
                assert!(reason.contains(";"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_narrative_paraphrase_retract_is_advisory() {
        // Ship 5a: cross_check.paraphrase_aware_match retracting
        // alone does NOT drive wire verdict (advisory only). The
        // structural compare in dont_fabricate is the load-bearing
        // factuality check. This test locks the merge semantics so
        // a future refactor doesn't accidentally re-enable
        // paraphrase as authoritative.
        let mut b = empty_narrative_breakdown();
        b.cross_check.paraphrase_aware_match = SubVerdict::Retracted {
            reason: "prose drift".into(),
        };
        assert!(matches!(merge_narrative(&b), PolicyVerdict::Approved));
    }

    // --- needs_llm_call -----------------------------------------------------

    #[test]
    fn needs_llm_call_true_when_stay_in_role_on() {
        let mut s = all_off_switches();
        s.stay_in_role = true;
        assert!(needs_llm_call(&s));
    }

    #[test]
    fn needs_llm_call_true_when_paraphrase_on() {
        let mut s = all_off_switches();
        s.cross_check.paraphrase_aware_match = true;
        assert!(needs_llm_call(&s));
    }

    #[test]
    fn needs_llm_call_false_when_no_llm_dependent_switch() {
        let mut s = all_off_switches();
        s.dont_fabricate = true;
        s.cross_check.ground_truth_match = true;
        assert!(!needs_llm_call(&s));
    }

    #[test]
    fn needs_llm_call_false_for_all_off() {
        assert!(!needs_llm_call(&all_off_switches()));
    }

    // --- ship 5a placeholder + structural legs -----------------------------
    // Unit-level coverage of the underlying functions lives in
    // `policy_placeholder` and `policy_structural`. These tests
    // exercise the policy.rs wrappers (`run_placeholder_leg_*`,
    // `run_structural_leg`) end-to-end.

    #[test]
    fn placeholder_leg_claim_approves_when_refs_resolve() {
        let claim = mk_claim(
            "Wallet ${ref:0}",
            "Wallet ${ref:0} has activity",
            vec![ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            }],
        );
        assert!(matches!(
            run_placeholder_leg_claim(&claim),
            SubVerdict::Approved
        ));
    }

    #[test]
    fn placeholder_leg_claim_retracts_on_out_of_bounds() {
        let claim = mk_claim(
            "Wallet ${ref:0}",
            "Wallet ${ref:5} has activity", // body refs index 5
            vec![ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            }],
        );
        match run_placeholder_leg_claim(&claim) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("out of bounds"));
            }
            other => panic!("expected Retracted, got {other:?}"),
        }
    }

    #[test]
    fn placeholder_leg_narrative_approves_with_no_refs_no_provenance() {
        // Pure descriptive narrative without citations: bare
        // commentary, no audit data claims. Approve unconditionally.
        let v = run_placeholder_leg_narrative(
            "the wallet has 3 distinguishing properties",
            &[],
        );
        assert!(matches!(v, SubVerdict::Approved));
    }

    #[test]
    fn structural_leg_approves_when_chips_trace() {
        let store = store_with_one_wallet_profile();
        let provenance = vec![
            ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            },
            ProvenanceRef::Number {
                metric: "volume".into(),
                value: 12.4,
                support: vec![],
            },
        ];
        assert!(matches!(
            run_structural_leg(&provenance, &store),
            SubVerdict::Approved
        ));
    }

    #[test]
    fn structural_leg_retracts_unsourced_number() {
        let store = store_with_one_wallet_profile();
        let provenance = vec![ProvenanceRef::Number {
            metric: "volume".into(),
            value: 50000.0, // way outside the captured 12.4
            support: vec![],
        }];
        match run_structural_leg(&provenance, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("does not trace"));
            }
            other => panic!("expected Retracted, got {other:?}"),
        }
    }

    #[test]
    fn structural_leg_retracts_unsourced_wallet() {
        let store = store_with_one_wallet_profile();
        let provenance = vec![ProvenanceRef::Wallet {
            addr: "FAKE_NEVER_SEEN".into(),
            idx: None,
        }];
        match run_structural_leg(&provenance, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("not returned by any primitive"));
            }
            other => panic!("expected Retracted, got {other:?}"),
        }
    }

    // --- PathBuilder --------------------------------------------------------

    #[test]
    fn path_builder_records_steps_in_order() {
        let switches = all_on_switches();
        let mut path = PathBuilder::new("narrative", &switches);
        path.record(
            "narrative.stay_in_role",
            &SubVerdict::Approved,
            "constitution rules approved".to_string(),
        );
        path.skip("narrative.dont_fabricate", "switch off");
        let gp = path.finish(PolicyVerdict::Approved);
        assert_eq!(gp.steps.len(), 2);
        assert_eq!(gp.steps[0].stage, "narrative.stay_in_role");
        assert_eq!(gp.steps[1].stage, "narrative.dont_fabricate");
    }

    #[test]
    fn path_builder_caps_at_max_steps() {
        let switches = all_on_switches();
        let mut path = PathBuilder::new("narrative", &switches);
        for i in 0..(MAX_PATH_STEPS + 5) {
            path.record(
                &format!("stage_{i}"),
                &SubVerdict::Approved,
                "n".to_string(),
            );
        }
        let gp = path.finish(PolicyVerdict::Approved);
        assert_eq!(gp.steps.len(), MAX_PATH_STEPS);
    }

    // --- guarded ------------------------------------------------------------

    #[test]
    fn guarded_skips_when_switch_off() {
        let switches = all_on_switches();
        let mut path = PathBuilder::new("narrative", &switches);
        let v = guarded(false, &mut path, "narrative.dont_fabricate", || {
            (
                SubVerdict::Retracted {
                    reason: "should not run".into(),
                },
                "shouldn't see this".to_string(),
            )
        });
        match v {
            SubVerdict::NotApplicable { detail } => assert_eq!(detail, "switch off"),
            _ => panic!("expected NotApplicable"),
        }
        let gp = path.finish(PolicyVerdict::Approved);
        assert_eq!(gp.steps.len(), 1);
        match &gp.steps[0].state {
            PathState::NotApplicable { detail } => assert_eq!(detail, "switch off"),
            _ => panic!("expected NotApplicable state"),
        }
    }

    #[test]
    fn guarded_runs_when_switch_on() {
        let switches = all_on_switches();
        let mut path = PathBuilder::new("narrative", &switches);
        let v = guarded(true, &mut path, "narrative.dont_fabricate", || {
            (SubVerdict::Approved, "ran".to_string())
        });
        assert!(matches!(v, SubVerdict::Approved));
        let gp = path.finish(PolicyVerdict::Approved);
        assert_eq!(gp.steps.len(), 1);
        assert_eq!(gp.steps[0].note, "ran");
    }
}
