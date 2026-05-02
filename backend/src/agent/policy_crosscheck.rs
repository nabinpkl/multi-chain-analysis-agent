//! Shared taxonomy + tolerance + LLM-extractor compare. Ship 5a
//! retired the regex-on-prose machinery this file used to host
//! (`extract_from_text`, `extract_from_claim`, `cross_check`,
//! `prepare_text`, `is_hedged_at`, `parse_match_value`,
//! `multiplier_factor`, `NUMBER_RE`, etc.). The surviving surface:
//!
//! - `UnitClass` and `ExtractedNumber`: shared classification
//!   vocabulary used by `binding_store`, the structural gate in
//!   `policy_structural`, and the LLM-extractor compare here.
//! - `CrosscheckConfig`: tolerance knobs (declarative + hedged).
//! - `within_tolerance`: pure float compare, used everywhere
//!   tolerance matters (binding store, structural gate, LLM-
//!   extractor compare, ship 4 diff walker).
//! - `classify_metric`: maps a metric name string to its UnitClass.
//!   Used by `binding_store::build_binding`, `policy_structural`,
//!   `LlmExtractedNumber::into_extracted`.
//! - `LlmExtractedNumber` + `cross_check_extracted_pair`: the
//!   constitution gate's extraction sidecar shape, plus the
//!   compare it feeds into for the (advisory in ship 5a) paraphrase
//!   cross-check. Still useful as a coherence signal even after
//!   factuality moved to structural compare.
//!
//! No regex on prose lives here anymore. Ship 5a's
//! `policy_placeholder` is the only regex caller in the gate, and
//! it operates on the deterministic `${ref:N}` ASCII grammar.

use serde::Deserialize;

use super::types::Claim;

/// Tunable knobs. Defaults match the ship 2.5 plan; iterate via
/// dogfood feedback before plumbing as env vars.
#[derive(Debug, Clone, Copy)]
pub struct CrosscheckConfig {
    pub declarative_tolerance: f64,
    pub hedged_tolerance: f64,
}

impl Default for CrosscheckConfig {
    fn default() -> Self {
        Self {
            declarative_tolerance: 0.10,
            hedged_tolerance: 0.15,
        }
    }
}

/// Unit class for a parsed number. Compared as equal-class only, so
/// "12,300 SOL" never matches "12,300 connections" even though the
/// raw values agree. `Lamports` and `Sol` collapse to a single class
/// (`Sol`) at extraction time so trillion-lamport vs thousand-SOL
/// comparisons survive float precision.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum UnitClass {
    /// SOL volumes (also where lamports normalize to, divided by 1e9).
    Sol,
    /// Edge / node / tx counts.
    Count,
    /// `community 42`, `#1977`. Match by exact value (tolerance
    /// effectively 0%), but typed so a community id never collides
    /// with a count.
    CommunityId,
    /// Bare number with no unit suffix recognized.
    Raw,
}

/// One extracted number ready for compare. `value` is the canonical
/// form (lamports already divided to SOL; multiplier suffixes already
/// expanded). Today these come from two sources: the LLM extractor
/// sidecar (via `LlmExtractedNumber::into_extracted`) and the binding
/// store walking primitive output (via `build_binding`). Ship 5a
/// removed the regex extractor that used to populate this from prose.
#[derive(Debug, Clone)]
pub struct ExtractedNumber {
    pub value: f64,
    pub unit_class: UnitClass,
    pub hedged: bool,
}

/// Reason a cross-check retracted. `to_human_string()` produces the
/// one-sentence text that flows into the wire `reason` field.
#[derive(Debug, Clone)]
pub enum RetractReason {
    Unsourced {
        value: f64,
        unit_class: UnitClass,
    },
}

impl RetractReason {
    pub fn to_human_string(&self) -> String {
        match self {
            RetractReason::Unsourced { value, unit_class } => {
                let unit_text = match unit_class {
                    UnitClass::Sol => " SOL",
                    UnitClass::Count => "",
                    UnitClass::CommunityId => " (community id)",
                    UnitClass::Raw => "",
                };
                format!(
                    "narrative number {}{} not found in cited Claims",
                    format_number(*value),
                    unit_text,
                )
            }
        }
    }
}

fn format_number(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

/// Compare two pre-extracted number sets. Used by the LLM extractor
/// path (constitution gate's extraction sidecar) for ship 5a's
/// advisory `paraphrase_aware_match` coherence check.
///
/// Returns `Ok(())` when every narrative number matches at least one
/// claim number OR `extra_source` number on the same `unit_class`
/// within tolerance. `extra_source` (ship 3) is the primitive-
/// binding store's number set; passing an empty slice preserves the
/// pre-ship-3 behavior. Returns the first unsourced narrative
/// number on `Err`.
///
/// Ship 5a note: this function is no longer load-bearing for
/// factuality. The structural placeholder + chip-value compare in
/// `policy_structural` is the load-bearing factuality check; this
/// remains as the coherence advisory under
/// `cross_check.paraphrase_aware_match`. Kept because the LLM
/// extractor sidecar produces typed pairs naturally (no regex
/// involvement) and the compare semantics are useful for surfacing
/// prose-vs-citation drift.
pub fn cross_check_extracted_pair(
    narrative_numbers: &[ExtractedNumber],
    claim_numbers: &[ExtractedNumber],
    extra_source: &[ExtractedNumber],
    config: CrosscheckConfig,
) -> Result<(), RetractReason> {
    if narrative_numbers.is_empty() {
        return Ok(());
    }
    for n in narrative_numbers {
        if !has_match(n, claim_numbers, config) && !has_match(n, extra_source, config) {
            return Err(RetractReason::Unsourced {
                value: n.value,
                unit_class: n.unit_class,
            });
        }
    }
    Ok(())
}

/// LLM-side extracted number, deserialized from the constitution
/// gate's `extraction` JSON sidecar. Maps cleanly to `ExtractedNumber`
/// for compare. The `phrase` field is debugging context only;
/// surfaced in dev-mode `debug_*` fields so the dev can verify what
/// the LLM thought it saw, and discarded during compare.
#[derive(Debug, Clone, Deserialize, serde::Serialize)]
pub struct LlmExtractedNumber {
    pub value: f64,
    pub unit_class: String,
    #[serde(default)]
    pub phrase: String,
}

impl LlmExtractedNumber {
    /// Map the string `unit_class` from the LLM into our enum.
    /// Unknown values fall back to `Raw` so the compare skips them
    /// rather than silently approving on a misclassification.
    pub fn into_extracted(&self) -> ExtractedNumber {
        let unit_class = match self.unit_class.to_ascii_lowercase().as_str() {
            "sol" => UnitClass::Sol,
            "count" => UnitClass::Count,
            "community_id" | "community-id" | "community" => UnitClass::CommunityId,
            _ => UnitClass::Raw,
        };
        ExtractedNumber {
            value: self.value,
            unit_class,
            // The LLM doesn't tell us hedged-vs-declarative; treat as
            // declarative (tighter tolerance) by default.
            hedged: false,
        }
    }
}

fn has_match(n: &ExtractedNumber, refs: &[ExtractedNumber], cfg: CrosscheckConfig) -> bool {
    let tol = if n.hedged {
        cfg.hedged_tolerance
    } else {
        cfg.declarative_tolerance
    };
    refs.iter()
        .any(|r| r.unit_class == n.unit_class && within_tolerance(n.value, r.value, tol))
}

/// Pure float compare with fractional tolerance. Public so callers
/// outside this module (binding store, ship 5a structural gate, ship
/// 4 diff walker) can reuse the same numeric semantics.
pub fn within_tolerance(narr: f64, claim: f64, frac: f64) -> bool {
    if claim == 0.0 {
        // Avoid divide-by-zero. Only match other zeros.
        return narr == 0.0;
    }
    ((narr - claim).abs() / claim.abs()) <= frac
}

/// Classify a metric string from `support_numbers`,
/// `ProvenanceRef::Number`, or a primitive output JSON field name to
/// a `UnitClass`. Conservative: unrecognized metric names go to `Raw`
/// so they don't accidentally satisfy a typed claim.
///
/// Public so ship 5a's `policy_structural` module + ship 3's
/// `binding_store::build_binding` reuse the same taxonomy.
pub fn classify_metric(metric: &str) -> UnitClass {
    let lower = metric.to_ascii_lowercase();
    if lower.contains("sol")
        || lower.contains("lamport")
        || lower.contains("volume")
        || lower.contains("inflow")
        || lower.contains("outflow")
        || lower.contains("inbound")
        || lower.contains("outbound")
    {
        UnitClass::Sol
    } else if lower.contains("count")
        || lower.contains("degree")
        || lower.contains("connection")
        || lower.contains("tx")
        || lower.contains("edge")
        || lower.contains("node")
    {
        UnitClass::Count
    } else if lower.contains("community") {
        UnitClass::CommunityId
    } else {
        UnitClass::Raw
    }
}

/// Imports are kept here only so callers that build `ExtractedNumber`s
/// from a `Claim`'s structured fields (ship 5a's `policy_structural`
/// uses this when iterating provenance) can do so via a single
/// helper. The function walks `claim.support_numbers` +
/// `claim.provenance::Number` entries; it does NOT regex any text.
pub fn structured_extract_from_claim(c: &Claim) -> Vec<ExtractedNumber> {
    use super::types::ProvenanceRef;
    let mut out = Vec::new();
    for n in &c.support_numbers {
        out.push(ExtractedNumber {
            value: n.value,
            unit_class: classify_metric(&n.metric),
            hedged: false,
        });
    }
    for p in &c.provenance {
        if let ProvenanceRef::Number { metric, value, .. } = p {
            out.push(ExtractedNumber {
                value: *value,
                unit_class: classify_metric(metric),
                hedged: false,
            });
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> CrosscheckConfig {
        CrosscheckConfig::default()
    }

    fn n(value: f64, unit_class: UnitClass) -> ExtractedNumber {
        ExtractedNumber {
            value,
            unit_class,
            hedged: false,
        }
    }

    // --- within_tolerance ---------------------------------------------------

    #[test]
    fn within_tolerance_exact_match() {
        assert!(within_tolerance(12.4, 12.4, 0.10));
    }

    #[test]
    fn within_tolerance_small_drift_ok() {
        assert!(within_tolerance(12.4, 12.5, 0.10)); // ~0.8% off
    }

    #[test]
    fn within_tolerance_large_drift_fails() {
        assert!(!within_tolerance(12.4, 50.0, 0.10));
    }

    #[test]
    fn within_tolerance_zero_only_matches_zero() {
        assert!(within_tolerance(0.0, 0.0, 0.10));
        assert!(!within_tolerance(0.001, 0.0, 0.10));
    }

    // --- classify_metric ---------------------------------------------------

    #[test]
    fn classify_sol_synonyms() {
        // classify_metric does substring contains() against a small
        // set of unit-suggestive tokens. Field names that include
        // those tokens classify as Sol.
        assert_eq!(classify_metric("volume"), UnitClass::Sol);
        assert_eq!(classify_metric("total_volume"), UnitClass::Sol);
        assert_eq!(classify_metric("inbound_volume"), UnitClass::Sol);
        assert_eq!(classify_metric("lamports"), UnitClass::Sol);
        assert_eq!(classify_metric("sol_inflow"), UnitClass::Sol);
        // The `*_volume_lamports` family used by `NodeStatsWire`
        // classifies as Sol (substring "volume" + "lamport"). The
        // wallet_profile primitive renamed its short keys
        // (`in_vol`/`out_vol`/`bidir_vol`) to the descriptive form
        // precisely so its binding-store entries land in Sol class
        // and the structural value-compare gate actually verifies
        // them instead of skipping via the Raw-class shortcut.
        assert_eq!(classify_metric("in_volume_lamports"), UnitClass::Sol);
        assert_eq!(classify_metric("out_volume_lamports"), UnitClass::Sol);
        assert_eq!(classify_metric("bidir_volume_lamports"), UnitClass::Sol);
        assert_eq!(classify_metric("total_volume_lamports"), UnitClass::Sol);
    }

    #[test]
    fn classify_count_synonyms() {
        assert_eq!(classify_metric("degree"), UnitClass::Count);
        assert_eq!(classify_metric("edge_count"), UnitClass::Count);
        assert_eq!(classify_metric("connections"), UnitClass::Count);
        assert_eq!(classify_metric("tx_count"), UnitClass::Count);
    }

    #[test]
    fn classify_community_id() {
        assert_eq!(classify_metric("community_id"), UnitClass::CommunityId);
        assert_eq!(classify_metric("community"), UnitClass::CommunityId);
    }

    #[test]
    fn classify_unknown_falls_to_raw() {
        assert_eq!(classify_metric("score"), UnitClass::Raw);
        assert_eq!(classify_metric("frobnicated_factor"), UnitClass::Raw);
    }

    // --- cross_check_extracted_pair ---------------------------------------

    #[test]
    fn extracted_pair_empty_narrative_approves() {
        let claims: Vec<ExtractedNumber> = vec![];
        assert!(cross_check_extracted_pair(&[], &claims, &[], cfg()).is_ok());
    }

    #[test]
    fn extracted_pair_match_in_claims_approves() {
        let narr = vec![n(12.4, UnitClass::Sol)];
        let claims = vec![n(12.5, UnitClass::Sol)];
        assert!(cross_check_extracted_pair(&narr, &claims, &[], cfg()).is_ok());
    }

    #[test]
    fn extracted_pair_match_in_extra_source_approves() {
        // Number not in claims but in primitive binding (extra_source).
        let narr = vec![n(33.0, UnitClass::Count)];
        let claims: Vec<ExtractedNumber> = vec![];
        let extra = vec![n(33.0, UnitClass::Count)];
        assert!(cross_check_extracted_pair(&narr, &claims, &extra, cfg()).is_ok());
    }

    #[test]
    fn extracted_pair_unsourced_retracts() {
        let narr = vec![n(50000.0, UnitClass::Sol)];
        let claims = vec![n(12.4, UnitClass::Sol)];
        match cross_check_extracted_pair(&narr, &claims, &[], cfg()) {
            Err(RetractReason::Unsourced { value, unit_class }) => {
                assert_eq!(value, 50000.0);
                assert_eq!(unit_class, UnitClass::Sol);
            }
            other => panic!("expected Unsourced; got {other:?}"),
        }
    }

    #[test]
    fn extracted_pair_unit_class_mismatch_retracts() {
        // Same value, different unit class: should NOT match.
        let narr = vec![n(33.0, UnitClass::Sol)];
        let claims = vec![n(33.0, UnitClass::Count)];
        let res = cross_check_extracted_pair(&narr, &claims, &[], cfg());
        assert!(res.is_err());
    }

    // --- LlmExtractedNumber ------------------------------------------------

    #[test]
    fn llm_extracted_known_classes() {
        let cases = vec![
            ("sol", UnitClass::Sol),
            ("count", UnitClass::Count),
            ("community_id", UnitClass::CommunityId),
            ("community", UnitClass::CommunityId),
        ];
        for (input, expected) in cases {
            let llm = LlmExtractedNumber {
                value: 1.0,
                unit_class: input.into(),
                phrase: "".into(),
            };
            let extracted = llm.into_extracted();
            assert_eq!(extracted.unit_class, expected, "input={input}");
        }
    }

    #[test]
    fn llm_extracted_unknown_falls_to_raw() {
        let llm = LlmExtractedNumber {
            value: 1.0,
            unit_class: "weight".into(),
            phrase: "".into(),
        };
        assert_eq!(llm.into_extracted().unit_class, UnitClass::Raw);
    }

    // --- structured_extract_from_claim ------------------------------------

    #[test]
    fn structured_extract_pulls_support_numbers_and_provenance() {
        use super::super::types::{ClaimKind, NumberRef, PolicyVerdict, ProvenanceRef};
        let claim = Claim {
            id: "t".into(),
            session_id: "s".into(),
            kind: ClaimKind::Profile,
            headline: "ignored by structured extract".into(),
            body_markdown: "ignored too".into(),
            provenance: vec![
                ProvenanceRef::Number {
                    metric: "volume".into(),
                    value: 12.4,
                    support: vec![],
                },
                ProvenanceRef::Number {
                    metric: "degree".into(),
                    value: 33.0,
                    support: vec![],
                },
                ProvenanceRef::Wallet {
                    addr: "AAA".into(),
                    idx: None,
                },
            ],
            support_numbers: vec![NumberRef {
                metric: "edge_count".into(),
                value: 88.0,
            }],
            subgraph_slice: None,
            policy_verdict: PolicyVerdict::Approved,
            stubs_active: vec![],
            emitted_at_ms: 0,
        };
        let out = structured_extract_from_claim(&claim);
        // 1 support_number + 2 provenance numbers; wallet ignored.
        assert_eq!(out.len(), 3);
        let units: Vec<UnitClass> = out.iter().map(|n| n.unit_class).collect();
        assert!(units.contains(&UnitClass::Sol));
        assert!(units.contains(&UnitClass::Count));
    }
}
