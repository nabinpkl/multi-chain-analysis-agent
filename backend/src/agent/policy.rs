//! Output-policy gate (phase 03 layer 3). Ship 2 promoted this from
//! the always-approve stub to a real cheap-model call against the
//! constitution. Ship 2.7 split narrative gating into three
//! independent verdicts. Ship 3 adds the fourth: a deterministic
//! binding leg that asserts numbers + provenance refs trace back to
//! actual primitive output, closing the fabrication gap surfaced in
//! ship 2.7's adversarial dogfood.
//!
//! The four legs:
//!
//! 1. **Regex extractor**  deterministic number extraction +
//!    tolerance compare. Fast (<1ms), brittle on paraphrase.
//! 2. **LLM extractor**  the constitution-gate's response carries
//!    an `extraction` JSON sidecar; we run the same deterministic
//!    compare on the LLM-extracted set. Robust to paraphrase, costs
//!    nothing extra (folded into existing call).
//! 3. **Constitution**  the cheap-model's verdict on Rules 1-6.
//! 4. **Binding** (ship 3)  every number in claim body must trace
//!    to a primitive output we actually returned; provenance refs
//!    must point at wallets / communities the runtime saw. For
//!    narrative: numbers must trace through the cited Claims OR
//!    primitive output, AND the store must be non-empty if the
//!    narrative contains numbers (no numbers without a fetch).
//!
//! Show-all-default-strict merge: any retract → wire retract; the
//! per-extractor breakdown is surfaced in dev-mode `debug_*` so
//! disagreement between extractors is visible inline.

use serde::{Deserialize, Serialize};
use tracing::{info, warn};

use super::client::AgentClient;
use super::policy_crosscheck::{
    cross_check, cross_check_extracted_pair, extract_from_text, CrosscheckConfig, ExtractedNumber,
    LlmExtractedNumber, UnitClass,
};
use super::policy_prompt::{POLICY_PROMPT_V3_TAG, POLICY_PROMPT_V3_TEXT};
use super::primitives::PrimitiveBindingStore;
use super::types::{Claim, PolicyVerdict, ProvenanceRef};

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
/// malformed or older-style v1/v2 response still parses cleanly 
/// `extraction = None` falls through to `SubVerdict::NotApplicable`
/// in the merge.
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

/// One leg of the four-verdict merge. `NotApplicable` is the
/// "couldn't run" state  used when the LLM extraction is missing
/// or the cheap model produced a malformed response. `NotApplicable`
/// contributes neither approve nor retract to the merge; the other
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
}

/// Per-extractor breakdown for narrative gating (ship 3: four legs).
/// Surfaced as the `debug_*` field on the SSE wire when
/// `AGENT_DEBUG_PUBLIC=1`, and as the `breakdown` field on the
/// ledger PolicyVerdict event for replay / eval.
#[derive(Debug, Clone, Serialize)]
pub struct FourVerdictResult {
    pub regex: SubVerdict,
    pub llm_extraction: SubVerdict,
    pub constitution: SubVerdict,
    /// Ship 3: deterministic check that narrative / claim numbers
    /// trace to primitive output and provenance refs cite real
    /// entities the runtime saw.
    pub binding: SubVerdict,
}

impl FourVerdictResult {
    /// Format a one-line human-readable summary of the breakdown.
    /// Goes into the SSE `debug_*` field when dev-mode is on.
    pub fn format_for_dev(&self) -> String {
        format!(
            "regex: {} | llm-extract: {} | constitution: {} | binding: {}",
            format_leg(&self.regex),
            format_leg(&self.llm_extraction),
            format_leg(&self.constitution),
            format_leg(&self.binding),
        )
    }
}

/// Per-claim breakdown (ship 3). Constitution + binding only;
/// regex / llm-extraction are narrative-vs-claim consistency
/// checks that don't apply to claims directly.
#[derive(Debug, Clone, Serialize)]
pub struct ClaimVerdictResult {
    pub constitution: SubVerdict,
    pub binding: SubVerdict,
}

impl ClaimVerdictResult {
    pub fn format_for_dev(&self) -> String {
        format!(
            "constitution: {} | binding: {}",
            format_leg(&self.constitution),
            format_leg(&self.binding),
        )
    }
}

fn format_leg(v: &SubVerdict) -> String {
    match v {
        SubVerdict::Approved => "approved".to_string(),
        SubVerdict::Retracted { reason } => format!("retracted ({reason})"),
        SubVerdict::NotApplicable { detail } => format!("n/a ({detail})"),
    }
}

/// Final orchestration result for a narrative gate. Loop reads
/// `verdict` for retry / SSE-frame control, hands `breakdown` to
/// the ledger, and uses `breakdown.format_for_dev()` for the
/// dev-mode SSE debug field. `raw_extraction` carries the LLM's
/// original number lists for ledger replay (ship 6 eval will query
/// these).
pub struct NarrativeGateResult {
    pub verdict: PolicyVerdict,
    pub breakdown: FourVerdictResult,
    pub raw_extraction: Option<LlmExtraction>,
}

/// Final orchestration result for a claim gate (ship 3).
pub struct ClaimGateResult {
    pub verdict: PolicyVerdict,
    pub breakdown: ClaimVerdictResult,
}

/// Output-policy gate. Owns a clone of the agent client so the
/// `policy_model` call site is one place.
///
/// `client` is `Option` so the gate can be constructed cleanly when
/// the agent feature is disabled (no `AGENT_API_KEY`): we still want
/// `AppState` to own a non-Option `OutputPolicy` so call sites stay
/// simple, but with no client we have nothing to call. In that mode,
/// gate methods auto-approve and a single warn fires at boot. The
/// agent HTTP endpoints already 503 when the client is missing, so in
/// practice the auto-approve path is unreachable from the loop.
pub struct OutputPolicy {
    client: Option<AgentClient>,
    constitution_tag: &'static str,
    constitution_text: &'static str,
}

impl OutputPolicy {
    pub fn new(client: Option<AgentClient>) -> Self {
        match client.as_ref() {
            Some(c) => info!(
                tag = POLICY_PROMPT_V3_TAG,
                len = POLICY_PROMPT_V3_TEXT.len(),
                policy_model = c.policy_model(),
                "policy gate online (constitution v3, four-leg merge)",
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
            constitution_tag: POLICY_PROMPT_V3_TAG,
            constitution_text: POLICY_PROMPT_V3_TEXT,
        }
    }

    /// Constitution version tag. Surfaced to the loop so it can write
    /// a `Prompt` ledger event noting which constitution gated this
    /// session (mirrors how the primary system prompt is logged).
    pub fn constitution_tag(&self) -> &'static str {
        self.constitution_tag
    }

    /// Verdict for a Claim about to be emitted. Sync gate per phase 03
    /// OQ-1 default; latency cost is paid before the SSE push so the
    /// frontend never sees an approved claim that's later retracted.
    /// Ship 3 extends this with the binding leg.
    pub async fn check_claim(
        &self,
        claim: &Claim,
        binding_store: &PrimitiveBindingStore,
    ) -> ClaimGateResult {
        let req = GateRequest {
            channel: "claim",
            payload: claim,
            same_turn_claims: &[],
        };
        let constitution_v = match serde_json::to_string(&req) {
            Ok(user) => {
                // For the claim channel the extraction sidecar is
                // ignored; the constitution sub-verdict alone goes
                // into the merge for this leg.
                let (sub, _extraction) = self.run_gate("claim", &user).await;
                sub
            }
            Err(e) => {
                warn!(error = %e, "policy: serialize claim payload failed");
                SubVerdict::Retracted {
                    reason: "policy serialize failure".into(),
                }
            }
        };

        let binding_v = run_binding_leg_claim(claim, binding_store);

        let breakdown = ClaimVerdictResult {
            constitution: constitution_v,
            binding: binding_v,
        };

        info!(
            target: "agent::policy::claim",
            constitution = %breakdown.constitution.label(),
            binding = %breakdown.binding.label(),
            claim_id = %claim.id,
            "claim gate merged",
        );

        let verdict = merge_claim(&breakdown);
        ClaimGateResult { verdict, breakdown }
    }

    /// Four-verdict narrative gate (ship 3). Runs all four legs 
    /// regex cross-check, LLM-extracted cross-check, the constitution
    /// gate, and the binding leg  and merges with show-all-default-
    /// strict (any retract → retract). Returns:
    /// - `verdict`: the merged final verdict (what goes on the wire).
    /// - `breakdown`: per-leg results for ledger + dev-mode debug.
    /// - `raw_extraction`: the LLM's number lists for replay (ledger).
    pub async fn check_narrative(
        &self,
        text: &str,
        same_turn_claims: &[Claim],
        thread_history_claims: &[Claim],
        binding_store: &PrimitiveBindingStore,
    ) -> NarrativeGateResult {
        // Stage A: regex cross-check. Ship 3: the binding store's
        // numbers join the source set so a narrative number that
        // paraphrases primitive output gets approved even when no
        // claim restates it.
        let mut all_claims: Vec<Claim> =
            Vec::with_capacity(same_turn_claims.len() + thread_history_claims.len());
        all_claims.extend(same_turn_claims.iter().cloned());
        all_claims.extend(thread_history_claims.iter().cloned());
        let binding_numbers = binding_store.all_numbers();
        let regex_v = match cross_check(
            text,
            &all_claims,
            &binding_numbers,
            CrosscheckConfig::default(),
        ) {
            Ok(()) => SubVerdict::Approved,
            Err(reason) => SubVerdict::Retracted {
                reason: reason.to_human_string(),
            },
        };

        // Stage B + C: single LLM call returns constitution verdict +
        // extraction sidecar.
        let payload = serde_json::json!({ "text": text });
        let req = GateRequest {
            channel: "narrative",
            payload: &payload,
            same_turn_claims,
        };
        let user = match serde_json::to_string(&req) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = %e, "policy: serialize narrative payload failed");
                let constitution_v = SubVerdict::Retracted {
                    reason: "policy serialize failure".into(),
                };
                let binding_v = run_binding_leg_narrative(text, binding_store);
                let breakdown = FourVerdictResult {
                    regex: regex_v,
                    llm_extraction: SubVerdict::NotApplicable {
                        detail: "no LLM call (serialize failure)".into(),
                    },
                    constitution: constitution_v,
                    binding: binding_v,
                };
                return NarrativeGateResult {
                    verdict: merge(&breakdown),
                    breakdown,
                    raw_extraction: None,
                };
            }
        };

        let (constitution_v, raw_extraction) = self.run_gate("narrative", &user).await;

        // Stage B (continued): feed the LLM-extracted set through
        // the same deterministic compare we use for regex, with the
        // binding-store numbers as an additional source.
        let llm_extraction_v = match raw_extraction.as_ref() {
            Some(extraction) => {
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

        // Stage D (ship 3): binding leg on narrative. Catches the
        // "narrative talks about numbers without any primitive
        // having been called" failure mode that regex + LLM extract
        // can't, because both treat absence-of-source as "no claim
        // matched" rather than "fabricated".
        let binding_v = run_binding_leg_narrative(text, binding_store);

        let breakdown = FourVerdictResult {
            regex: regex_v,
            llm_extraction: llm_extraction_v,
            constitution: constitution_v,
            binding: binding_v,
        };
        let verdict = merge(&breakdown);

        info!(
            target: "agent::policy::narrative",
            regex = %breakdown.regex.label(),
            llm_extraction = %breakdown.llm_extraction.label(),
            constitution = %breakdown.constitution.label(),
            binding = %breakdown.binding.label(),
            same_turn = same_turn_claims.len(),
            thread_history = thread_history_claims.len(),
            bindings = binding_store.len(),
            "narrative gate merged",
        );

        NarrativeGateResult {
            verdict,
            breakdown,
            raw_extraction,
        }
    }

    /// Shared call site: hands the constitution + payload to the cheap
    /// model, parses the response, returns (constitution sub-verdict,
    /// optional extraction sidecar). Fail-closed on the constitution
    /// leg if anything is weird; extraction defaults to None.
    async fn run_gate(
        &self,
        channel: &str,
        user: &str,
    ) -> (SubVerdict, Option<LlmExtraction>) {
        let client = match &self.client {
            Some(c) => c,
            None => {
                // No client = agent disabled. Auto-approve so the
                // (unreachable from real endpoints) call site doesn't
                // stall. The boot-time warn already named this.
                return (SubVerdict::Approved, None);
            }
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

        // Some cheap models wrap the JSON in markdown fences or add a
        // trailing apology. Be permissive: find the first balanced
        // JSON object substring and parse that.
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
// Binding leg
// ============================================================================

/// Narrative-side binding check (ship 3). Catches the case where the
/// narrative contains numeric content but no primitive has been
/// called this thread. The regex + LLM extract legs use the binding
/// store as a source set already; this leg is the "contains numbers
/// AND no source at all" guard, which neither can express.
fn run_binding_leg_narrative(text: &str, store: &PrimitiveBindingStore) -> SubVerdict {
    let extracted = extract_from_text(text);
    let has_audit_numbers = extracted
        .iter()
        .any(|n| !matches!(n.unit_class, UnitClass::Raw));
    if !has_audit_numbers {
        // Pure interpretation, or only address-digit / year / id
        // noise that the regex already filtered to Raw. Approve.
        return SubVerdict::Approved;
    }
    if store.is_empty() {
        return SubVerdict::Retracted {
            reason: "narrative contains numbers but no primitive output captured".into(),
        };
    }
    SubVerdict::Approved
}

/// Claim-side binding check (ship 3). Two responsibilities:
/// 1. Every audit-class number in claim headline + body must trace
///    to a binding number on the same unit class within tolerance.
/// 2. Every Wallet / Community provenance ref must point at an
///    entity the binding store recorded; TimeRange refs require the
///    store to have at least one TimeRange-bearing primitive.
fn run_binding_leg_claim(claim: &Claim, store: &PrimitiveBindingStore) -> SubVerdict {
    if store.is_empty() {
        // The model is emitting a Claim before any primitive
        // returned. There's no source for any number it's about to
        // cite. Distinguish this from "store has data but doesn't
        // cover this claim" so the dev sees the difference in the
        // breakdown.
        return SubVerdict::Retracted {
            reason: "claim emitted with empty binding store (no primitive output to cite)".into(),
        };
    }

    // Number tracing on combined headline + body_markdown.
    let mut combined = String::with_capacity(claim.headline.len() + claim.body_markdown.len() + 1);
    combined.push_str(&claim.headline);
    combined.push('\n');
    combined.push_str(&claim.body_markdown);
    let claim_text_numbers = extract_from_text(&combined);
    let store_numbers = store.all_numbers();
    let cfg = CrosscheckConfig::default();

    for n in &claim_text_numbers {
        if matches!(n.unit_class, UnitClass::Raw) {
            // Raw numbers (years, addresses-with-digits, bare small
            // integers) aren't audited  same convention as the
            // regex extractor's `small_bare_integer_skipped`.
            continue;
        }
        let matched = store_numbers
            .iter()
            .any(|src| src.unit_class == n.unit_class && within_tolerance(n.value, src.value, &cfg, n.hedged));
        if !matched {
            return SubVerdict::Retracted {
                reason: format!(
                    "claim number {} ({}) not sourced from primitive output",
                    format_value(n.value),
                    fmt_unit(n.unit_class),
                ),
            };
        }
    }

    // Provenance entity validation.
    let store_wallets = store.all_wallets();
    let store_communities = store.all_communities();
    for r in &claim.provenance {
        match r {
            ProvenanceRef::Wallet { addr, .. } => {
                if !store_wallets.contains(addr) {
                    return SubVerdict::Retracted {
                        reason: format!(
                            "provenance wallet {} not in primitive output",
                            short_addr(addr),
                        ),
                    };
                }
            }
            ProvenanceRef::Community { id } => {
                if !store_communities.contains(id) {
                    return SubVerdict::Retracted {
                        reason: format!(
                            "provenance community {} not in primitive output",
                            id,
                        ),
                    };
                }
            }
            ProvenanceRef::TimeRange { .. } => {
                if !store.has_any_time_range() {
                    return SubVerdict::Retracted {
                        reason: "provenance time-range not in primitive output".into(),
                    };
                }
            }
            // Edge refs and Number refs are accepted as-is for v0;
            // structurally any Edge cited must come from primitive
            // output too, but the live graph doesn't yet surface
            // edge ids the model can fabricate from. Number refs
            // already feed into the binding store's number set so
            // the number-tracing pass above covers them.
            ProvenanceRef::Edge { .. } | ProvenanceRef::Number { .. } => {}
        }
    }

    SubVerdict::Approved
}

fn within_tolerance(a: f64, b: f64, cfg: &CrosscheckConfig, hedged: bool) -> bool {
    let frac = if hedged {
        cfg.hedged_tolerance
    } else {
        cfg.declarative_tolerance
    };
    if b == 0.0 {
        return a == 0.0;
    }
    ((a - b).abs() / b.abs()) <= frac
}

fn fmt_unit(u: UnitClass) -> &'static str {
    match u {
        UnitClass::Sol => "sol",
        UnitClass::Count => "count",
        UnitClass::CommunityId => "community_id",
        UnitClass::Raw => "raw",
    }
}

fn format_value(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

fn short_addr(addr: &str) -> String {
    if addr.len() > 12 {
        format!("{}…{}", &addr[..6], &addr[addr.len() - 4..])
    } else {
        addr.to_string()
    }
}

// ============================================================================
// Merge
// ============================================================================

/// Show-all-default-strict merge of four narrative verdicts. Any
/// retract on any leg → retract on the wire. Approves only when no
/// leg flagged (NotApplicable counts as "no flag"). Reasons are
/// concatenated so the wire's `reason` string names every leg that
/// flagged.
fn merge(r: &FourVerdictResult) -> PolicyVerdict {
    let mut retract_reasons: Vec<String> = Vec::new();
    push_if_retract(&r.regex, "regex", &mut retract_reasons);
    push_if_retract(&r.llm_extraction, "llm-extract", &mut retract_reasons);
    push_if_retract(&r.constitution, "constitution", &mut retract_reasons);
    push_if_retract(&r.binding, "binding", &mut retract_reasons);
    if retract_reasons.is_empty() {
        PolicyVerdict::Approved
    } else {
        PolicyVerdict::Retracted {
            reason: retract_reasons.join("; "),
        }
    }
}

fn merge_claim(r: &ClaimVerdictResult) -> PolicyVerdict {
    let mut retract_reasons: Vec<String> = Vec::new();
    push_if_retract(&r.constitution, "constitution", &mut retract_reasons);
    push_if_retract(&r.binding, "binding", &mut retract_reasons);
    if retract_reasons.is_empty() {
        PolicyVerdict::Approved
    } else {
        PolicyVerdict::Retracted {
            reason: retract_reasons.join("; "),
        }
    }
}

fn push_if_retract(v: &SubVerdict, label: &str, out: &mut Vec<String>) {
    if let SubVerdict::Retracted { reason } = v {
        out.push(format!("{label}: {reason}"));
    }
}

/// Extract the first balanced `{...}` substring. Used when the cheap
/// model wraps its JSON in markdown fences or prefixes it with
/// boilerplate. Returns `None` if no balanced object is found.
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

/// Cap the policy-response string we put in logs so a runaway model
/// doesn't blow up tracing. 240 bytes is enough to debug parse issues
/// without filling disk.
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::primitives::{build_binding, PrimitiveBinding, PrimitiveBindingStore};
    use crate::agent::types::{ClaimKind, NumberRef};
    use serde_json::json;

    fn empty_breakdown() -> FourVerdictResult {
        FourVerdictResult {
            regex: SubVerdict::Approved,
            llm_extraction: SubVerdict::Approved,
            constitution: SubVerdict::Approved,
            binding: SubVerdict::Approved,
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

    #[test]
    fn merge_all_approved() {
        assert!(matches!(merge(&empty_breakdown()), PolicyVerdict::Approved));
    }

    #[test]
    fn merge_one_retract_propagates() {
        let mut r = empty_breakdown();
        r.regex = SubVerdict::Retracted {
            reason: "narrative number 33 not found in cited Claims".into(),
        };
        match merge(&r) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("regex: narrative number 33"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_binding_retract_propagates() {
        let mut r = empty_breakdown();
        r.binding = SubVerdict::Retracted {
            reason: "claim number 50000 (sol) not sourced from primitive output".into(),
        };
        match merge(&r) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("binding: claim number 50000"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_multiple_retracts_concatenated() {
        let r = FourVerdictResult {
            regex: SubVerdict::Retracted { reason: "X".into() },
            llm_extraction: SubVerdict::Retracted { reason: "Y".into() },
            constitution: SubVerdict::Approved,
            binding: SubVerdict::Retracted { reason: "Z".into() },
        };
        match merge(&r) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("regex: X"));
                assert!(reason.contains("llm-extract: Y"));
                assert!(reason.contains("binding: Z"));
                assert!(reason.contains(";"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_not_applicable_does_not_block() {
        let mut r = empty_breakdown();
        r.llm_extraction = SubVerdict::NotApplicable {
            detail: "extraction missing".into(),
        };
        assert!(matches!(merge(&r), PolicyVerdict::Approved));
    }

    #[test]
    fn format_for_dev_includes_all_four_legs() {
        let r = FourVerdictResult {
            regex: SubVerdict::Retracted {
                reason: "33 missing".into(),
            },
            llm_extraction: SubVerdict::Approved,
            constitution: SubVerdict::NotApplicable {
                detail: "parse fail".into(),
            },
            binding: SubVerdict::Approved,
        };
        let s = r.format_for_dev();
        assert!(s.contains("regex: retracted (33 missing)"));
        assert!(s.contains("llm-extract: approved"));
        assert!(s.contains("constitution: n/a (parse fail)"));
        assert!(s.contains("binding: approved"));
    }

    #[test]
    fn binding_leg_narrative_no_numbers_approves_empty_store() {
        // Pure-interpretation narrative + empty store should NOT
        // retract; the binding leg only triggers when there are
        // audit-class numbers in the prose.
        let store = PrimitiveBindingStore::new();
        let v = run_binding_leg_narrative(
            "this wallet appears to be acting as a hub",
            &store,
        );
        assert!(matches!(v, SubVerdict::Approved));
    }

    #[test]
    fn binding_leg_narrative_numbers_with_empty_store_retracts() {
        let store = PrimitiveBindingStore::new();
        let v = run_binding_leg_narrative(
            "the wallet has 33 connections in the live window",
            &store,
        );
        match v {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("no primitive output captured"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn binding_leg_narrative_numbers_with_populated_store_approves() {
        let store = store_with_one_wallet_profile();
        // Narrative restates a value the store has.
        let v = run_binding_leg_narrative(
            "the wallet has 33 connections in the live window",
            &store,
        );
        assert!(matches!(v, SubVerdict::Approved));
    }

    #[test]
    fn binding_leg_claim_empty_store_retracts() {
        let store = PrimitiveBindingStore::new();
        let claim = mk_claim(
            "Wallet A has 33 connections",
            "...with 33 connections in the live window",
            vec![ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            }],
        );
        match run_binding_leg_claim(&claim, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("empty binding store"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn binding_leg_claim_fabricated_number_retracts() {
        let store = store_with_one_wallet_profile();
        // Store has degree 33, volume 12.4. Claim invents 50000 SOL.
        let claim = mk_claim(
            "Wallet A moved 50000 SOL",
            "Wallet A moved 50,000 SOL inbound this window",
            vec![ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            }],
        );
        match run_binding_leg_claim(&claim, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("not sourced from primitive output"));
                assert!(reason.contains("50000"));
            }
            other => panic!("expected Retracted, got {other:?}"),
        }
    }

    #[test]
    fn binding_leg_claim_sourced_numbers_approve() {
        let store = store_with_one_wallet_profile();
        let claim = mk_claim(
            "Wallet A has degree 33",
            "Wallet A has 33 connections and 12.4 SOL volume in community 42",
            vec![ProvenanceRef::Wallet {
                addr: "AAA".into(),
                idx: None,
            }],
        );
        match run_binding_leg_claim(&claim, &store) {
            SubVerdict::Approved => {}
            other => panic!("expected Approved, got {other:?}"),
        }
    }

    #[test]
    fn binding_leg_claim_unseen_wallet_in_provenance_retracts() {
        let store = store_with_one_wallet_profile();
        let claim = mk_claim(
            "summary",
            "claim about wallet B",
            vec![ProvenanceRef::Wallet {
                addr: "BBB_NEVER_RETURNED".into(),
                idx: None,
            }],
        );
        match run_binding_leg_claim(&claim, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(reason.contains("provenance wallet"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn binding_leg_claim_unseen_community_in_provenance_retracts() {
        let store = store_with_one_wallet_profile();
        // Body intentionally has no numeric content so the number-
        // tracing pass approves and we land in the provenance-ref
        // check, which is what this test exercises.
        let claim = mk_claim(
            "summary",
            "claim about an unrelated community",
            vec![ProvenanceRef::Community { id: 9999 }],
        );
        match run_binding_leg_claim(&claim, &store) {
            SubVerdict::Retracted { reason } => {
                assert!(
                    reason.contains("provenance community 9999"),
                    "unexpected reason: {reason}",
                );
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_claim_constitution_only() {
        // Two-legged claim merge: only constitution + binding.
        let v = ClaimVerdictResult {
            constitution: SubVerdict::Retracted {
                reason: "rule X".into(),
            },
            binding: SubVerdict::Approved,
        };
        match merge_claim(&v) {
            PolicyVerdict::Retracted { reason } => {
                assert!(reason.contains("constitution: rule X"));
                assert!(!reason.contains("binding"));
            }
            _ => panic!("expected Retracted"),
        }
    }

    #[test]
    fn merge_claim_both_approved() {
        let v = ClaimVerdictResult {
            constitution: SubVerdict::Approved,
            binding: SubVerdict::Approved,
        };
        assert!(matches!(merge_claim(&v), PolicyVerdict::Approved));
    }

    // Silence the unused-import warning for the ledger field.
    #[test]
    fn binding_serialization_includes_call_id() {
        let b = PrimitiveBinding {
            call_id: "wp:abc".into(),
            primitive: "wallet_profile".into(),
            captured_at_ms: 1,
            provenance: vec![],
            numbers: vec![],
            entities: Default::default(),
        };
        let v = serde_json::to_value(&b).unwrap();
        assert_eq!(v["call_id"], "wp:abc");
        assert_eq!(v["primitive"], "wallet_profile");
    }
}
