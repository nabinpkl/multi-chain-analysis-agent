//! Deterministic numerical cross-check between Narrative prose and the
//! agent's own cited Claims. Ship 2.5's audit layer: enforces the
//! constitution v2 Rule 5 ("numbers in narrative come from cited
//! Claims; no calculation") with regex extraction + tolerance compare,
//! not a second LLM. Numbers are exactly where LLMs underperform code,
//! so the cross-check is pure functions, no model in the loop.
//!
//! The model's role is to comply with prompt v2 + constitution Rule 5
//! (don't compute, just restate). The cross-check is the safety net
//! that catches drift even when the primary slips.
//!
//! Public entry: `cross_check(narrative, claims, extra_source, config)
//! -> Result<(), RetractReason>`. `extra_source` (ship 3) carries
//! primitive-binding numbers; pass `&[]` for callers that only want
//! claim-driven cross-check.
//! Returns `Ok(())` if every extracted narrative number has a match
//! within tolerance in at least one cited Claim's number set; returns
//! `Err(RetractReason)` naming the first un-sourced number found.
//!
//! # Approve-on-extraction-failure
//!
//! When the regex doesn't produce a clean number from a substring
//! (ambiguous form, word-form like "fifty thousand", non-Latin
//! numerals, etc.), the extractor skips that token. We trade some
//! audit coverage for lower false-retract rate; the cheap-model
//! constitution gate still runs after, so a clear violation has two
//! chances to fail.
//!
//! # Tolerance
//!
//! Default `±10%` for declarative numbers, `±15%` for hedged ones
//! ("about 5k", "roughly 12k"). Tunable via `CrosscheckConfig`.

use std::sync::LazyLock;

use regex::Regex;
use serde::Deserialize;

use super::types::{Claim, ProvenanceRef};

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
/// expanded).
#[derive(Debug, Clone)]
pub struct ExtractedNumber {
    pub value: f64,
    pub unit_class: UnitClass,
    pub hedged: bool,
}

/// Reason for a cross-check retraction. `to_human_string()` produces
/// the one-sentence text that flows into the SSE
/// `NarrativeRetracted.reason` field and the ledger.
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
    // Tight rendering: integer if whole, else 2-3 sig digits.
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

/// Public entry point for the regex extractor. Walks `narrative`
/// extracting numbers via regex, walks every `claim` extracting from
/// `headline`, `body_markdown`, `support_numbers[]`, and
/// `provenance[].Number{value}`, then runs the shared compare via
/// `cross_check_extracted_pair`. Returns `Err` if any narrative
/// number lacks a same-unit-class match within tolerance.
///
/// `extra_source` carries primitive-binding numbers (ship 3) so
/// narrative numbers paraphrasing real primitive output get
/// approved even when the model didn't restate the value inside a
/// Claim's prose. Pass an empty slice for callers that don't want
/// that behavior.
pub fn cross_check(
    narrative: &str,
    claims: &[Claim],
    extra_source: &[ExtractedNumber],
    config: CrosscheckConfig,
) -> Result<(), RetractReason> {
    let narr_numbers = extract_from_text(narrative);
    let claim_numbers: Vec<ExtractedNumber> =
        claims.iter().flat_map(extract_from_claim).collect();
    cross_check_extracted_pair(&narr_numbers, &claim_numbers, extra_source, config)
}

/// Compare two pre-extracted number sets. Shared by the regex
/// extractor (`cross_check`) and the LLM extractor (ship 2.7,
/// `OutputPolicy::check_narrative`'s llm_extract path). Decoupling
/// the compare from extraction means both paths use the same
/// tolerance + unit-class semantics, so disagreement between them
/// is a real disagreement on what was extracted, not on what
/// matching means.
///
/// Returns `Ok(())` when every narrative number matches at least one
/// claim number OR `extra_source` number on the same `unit_class`
/// within tolerance. `extra_source` (ship 3) is the primitive-
/// binding store's number set; passing an empty slice preserves the
/// pre-ship-3 behavior. Returns the first unsourced narrative
/// number on `Err`.
pub fn cross_check_extracted_pair(
    narrative_numbers: &[ExtractedNumber],
    claim_numbers: &[ExtractedNumber],
    extra_source: &[ExtractedNumber],
    config: CrosscheckConfig,
) -> Result<(), RetractReason> {
    if narrative_numbers.is_empty() {
        // No numbers in narrative -> trivially passes.
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

/// LLM-side extracted number, deserialized from the constitution v3
/// `extraction` JSON sidecar. Maps cleanly to `ExtractedNumber` for
/// compare. The `phrase` field is debugging context only  surfaced
/// in dev-mode `debug_*` fields so the dev can verify what the LLM
/// thought it saw  and discarded during compare.
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
            // declarative (tighter tolerance) by default. The phrase
            // field could in principle be inspected for hedge
            // markers, but the small win isn't worth the complexity
            // until dogfood demands it.
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

fn within_tolerance(narr: f64, claim: f64, frac: f64) -> bool {
    if claim == 0.0 {
        // Avoid divide-by-zero. Only match other zeros.
        return narr == 0.0;
    }
    ((narr - claim).abs() / claim.abs()) <= frac
}

/// Extract every cross-check-able number from a single Claim. Pulls
/// from four sources:
/// 1. `support_numbers[]` (structured; mostly empty in dogfood but
///    populated when set).
/// 2. `provenance[].Number{value, metric}` (also structured).
/// 3. `body_markdown` (regex; the primary source in practice).
/// 4. `headline` (regex; one-line prose, often has the headline number).
pub fn extract_from_claim(c: &Claim) -> Vec<ExtractedNumber> {
    let mut out = Vec::new();
    // Structured: support_numbers. Metric name hints unit class.
    for n in &c.support_numbers {
        out.push(ExtractedNumber {
            value: n.value,
            unit_class: classify_metric(&n.metric),
            hedged: false,
        });
    }
    // Structured: ProvenanceRef::Number entries.
    for p in &c.provenance {
        if let ProvenanceRef::Number { metric, value, .. } = p {
            out.push(ExtractedNumber {
                value: *value,
                unit_class: classify_metric(metric),
                hedged: false,
            });
        }
    }
    // Prose: extract from headline + body. Ground-truth body is
    // typically richer than support_numbers in practice.
    out.extend(extract_from_text(&c.headline));
    out.extend(extract_from_text(&c.body_markdown));
    out
}

/// Classify a metric string from `support_numbers` or
/// `ProvenanceRef::Number` to a `UnitClass`. Conservative: unrecognized
/// metric names go to `Raw` so they don't accidentally satisfy a
/// typed claim.
fn classify_metric(metric: &str) -> UnitClass {
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

// ============================================================================
// Regex extraction
// ============================================================================

/// Hedge markers that widen the tolerance window. Detected as a
/// substring within a window before the matched number; see
/// `is_hedged_at`. Lowercased.
const HEDGE_MARKERS: &[&str] = &[
    "about ",
    "approx",
    "approximately ",
    "around ",
    "roughly ",
    "nearly ",
    "almost ",
    "close to ",
    "~",
    "≈",
    "circa ",
    "ca. ",
];

/// Master number regex. Matches (in alternation order):
///   - Scientific notation: `1.5e9`, `1.2×10^13`, `1.2 x 10 13` (after
///     superscripts have been ASCII-folded by `prepare_text`).
///   - Plain decimal/integer with optional grouping commas + optional
///     multiplier suffix: `1,234.5`, `5.1k`, `1.2M`, `1.5B`,
///     `12 trillion`, `302999700`, `12,300`.
///
/// Single capture group `num` handles both comma-grouped and bare
/// digit forms; the comma group is optional and `\d+` runs through
/// the whole digit string when no commas are present (avoids the
/// "matched only the first 3 digits" trap of separate alternatives).
///
/// `regex` crate alternations are leftmost-first; the more-specific
/// scientific patterns come first so they win over the bare-number
/// alt. `extract_from_text` calls `prepare_text` first to fold
/// `¹²³⁴⁵⁶⁷⁸⁹⁰` and `×` into ASCII so plain `\d` works.
static NUMBER_RE: LazyLock<Regex> = LazyLock::new(|| {
    // r#"..."# delimiters because the embedded comments contain
    // ASCII double-quotes around words like "t" / "trillion".
    Regex::new(
        r#"(?xi)
        # scientific notation a x 10 ^ b (x and ^ already
        # ASCII-folded; superscripts already converted to digits).
        (?P<sci>
            -?\d+(?:\.\d+)?
            \s*x\s*10\s*\^?\s*
            -?\d+
        )
        |
        # e-notation 1.5e9 1e6
        (?P<enot>-?\d+(?:\.\d+)?[eE][+-]?\d+)
        |
        # number with optional grouping commas and optional decimal,
        # followed by an optional multiplier suffix word. Multiplier
        # alternatives are LONGEST-FIRST: regex alternation is
        # leftmost-first, so a single-letter alternative listed
        # before a long-word alternative would capture only the
        # single letter and leave the rest in the tail (which
        # confuses the immediate-token classifier). Long words go
        # first; single letters go last.
        (?P<num>-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)
        \s*
        (?P<mult>trillion|thousand|million|billion|k|m|b|t)?
        "#,
    )
    .expect("static cross-check number regex compiles")
});

/// Pre-process text before regex extraction: fold unicode superscript
/// digits to ASCII (so `1.2×10¹³` becomes `1.2x10 13`) and the `×`
/// glyph to ASCII `x`. Operations are character-aligned so byte
/// offsets stay valid (each replaced char goes to a single ASCII
/// char). Returns an owned String so callers don't have to manage
/// the substitution.
fn prepare_text(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        let mapped = match c {
            '⁰' => '0',
            '¹' => '1',
            '²' => '2',
            '³' => '3',
            '⁴' => '4',
            '⁵' => '5',
            '⁶' => '6',
            '⁷' => '7',
            '⁸' => '8',
            '⁹' => '9',
            '×' => 'x',
            other => other,
        };
        out.push(mapped);
    }
    out
}

/// Word multipliers map.
fn multiplier_factor(suffix: &str) -> f64 {
    match suffix.to_ascii_lowercase().as_str() {
        "k" | "thousand" => 1e3,
        "m" | "million" => 1e6,
        "b" | "billion" => 1e9,
        "t" | "trillion" => 1e12,
        _ => 1.0,
    }
}

/// Determine if the substring before `idx` (within ~30 chars) ends with
/// any hedge marker. Cheap, lower-cased.
fn is_hedged_at(text_lower: &str, idx: usize) -> bool {
    let start = idx.saturating_sub(30);
    let window = &text_lower[start..idx];
    HEDGE_MARKERS.iter().any(|m| window.ends_with(m))
}

/// Extract every recognizable number from a free-text string.
///
/// Output: one `ExtractedNumber` per match. Order is best-effort
/// (regex left-to-right). Multiple numbers in one string produce one
/// entry each.
///
/// Approve-on-extraction-failure means: anything the regex doesn't
/// match (word-form numbers, non-Latin numerals, malformed) is
/// silently dropped. The constitution gate runs after as the second
/// layer.
pub fn extract_from_text(s: &str) -> Vec<ExtractedNumber> {
    let prepared = prepare_text(s);
    let lower = prepared.to_ascii_lowercase();
    let mut out = Vec::new();

    for cap in NUMBER_RE.captures_iter(&prepared) {
        let m = cap.get(0).unwrap();
        let raw = m.as_str();
        let value = match parse_match_value(&cap) {
            Some(v) => v,
            None => continue,
        };
        if !value.is_finite() || value < 0.0 {
            continue;
        }

        // Apply multiplier suffix.
        let mult = cap
            .name("mult")
            .map(|m| multiplier_factor(m.as_str()))
            .unwrap_or(1.0);
        let mut value = value * mult;

        // Classify by the token IMMEDIATELY after the number, not by
        // scanning the rest of the sentence. The old approach
        // (`take_while(!ascii_punct)`) ran past the number through
        // arbitrary alphanumeric content until punctuation, which
        // poisoned classification: a digit inside a wallet address
        // ("fueL3hBZj...") would extend its tail through "...zero
        // SOL movement..." 50+ chars later, classifying the digit as
        // SOL and triggering false retracts. Caught in dogfood ship
        // 2.6 (the "narrative number 3 SOL not found" mystery).
        //
        // Immediate-token rule: skip whitespace after the match, then
        // read alphabetic chars until the next non-letter. That
        // single token is the unit. "12 SOL" → "SOL", "12k SOL" →
        // mult eats "k", then space then "SOL", "fueL3hBZ" →
        // "hBZjLLL..." which isn't a recognized unit, so unit class
        // is Raw and `small_bare_integer_skipped` filters it out.
        let tail_start = m.end();
        let tail_str = &prepared[tail_start..];
        let after_ws = tail_str.trim_start();
        let immediate_token: String = after_ws
            .chars()
            .take_while(|c| c.is_ascii_alphabetic())
            .collect();
        let immediate_token_lower = immediate_token.to_ascii_lowercase();

        // Check what came BEFORE the number for community / context.
        // Walk back char-aligned (NOT byte-aligned) so we never slice
        // mid-codepoint on real model output, which routinely
        // contains smart quotes, em-dashes, NBSPs, etc. Byte-slicing
        // those panics; char_indices() gives us a stable boundary.
        let pre_start_byte = lower[..m.start()]
            .char_indices()
            .rev()
            .nth(19)
            .map(|(i, _)| i)
            .unwrap_or(0);
        let pre = &lower[pre_start_byte..m.start()];

        let mut unit_class = if immediate_token_lower == "sol" {
            UnitClass::Sol
        } else if immediate_token_lower.starts_with("lamport") {
            // Canonicalize lamports into SOL space to keep float
            // values small (avoid 1e13 vs 12300 comparisons).
            value /= 1e9;
            UnitClass::Sol
        } else if matches!(
            immediate_token_lower.as_str(),
            "connections" | "connection" | "counterparties" | "counterparty"
                | "tx" | "txs" | "edges" | "edge" | "nodes" | "node" | "degree"
        ) {
            UnitClass::Count
        } else {
            UnitClass::Raw
        };

        // Override: pre-context "community" + small int → CommunityId.
        if pre.contains("community") || pre.contains("comm.") {
            unit_class = UnitClass::CommunityId;
        }
        // Override: pre-context "degree of <N>" → Count.
        if pre.contains("degree of ") || pre.ends_with("degree ") {
            unit_class = UnitClass::Count;
        }

        let hedged = is_hedged_at(&lower, m.start());

        // Skip the trivial match where the regex picked up a tiny
        // standalone digit that's actually part of a year, address,
        // or stub identifier. Heuristic: bare 1-3 digit integer with
        // unit_class Raw is too noisy to audit; downstream sources
        // (community id, degree, etc.) get overridden above.
        if matches!(unit_class, UnitClass::Raw)
            && raw.len() < 4
            && !raw.contains('.')
        {
            continue;
        }

        out.push(ExtractedNumber {
            value,
            unit_class,
            hedged,
        });
    }
    out
}

/// Parse the matched capture group into a numeric value. Returns
/// `None` on parse failure (we silently skip; approve-on-uncertain).
fn parse_match_value(cap: &regex::Captures) -> Option<f64> {
    if let Some(sci) = cap.name("sci") {
        return parse_scientific_xnotation(sci.as_str());
    }
    if let Some(en) = cap.name("enot") {
        return en.as_str().parse::<f64>().ok();
    }
    if let Some(n) = cap.name("num") {
        let cleaned = n.as_str().replace(',', "");
        return cleaned.parse::<f64>().ok();
    }
    None
}

/// Parse "1.2x10^13" / "1.2 x 10 13" forms. Input is already
/// ASCII-folded by `prepare_text` (× → x, superscripts → digits) so
/// this just splits on `x`, parses mantissa, parses exponent.
fn parse_scientific_xnotation(s: &str) -> Option<f64> {
    let normalized = s.replace('^', "").to_ascii_lowercase();
    let parts: Vec<&str> = normalized.split('x').map(str::trim).collect();
    if parts.len() != 2 {
        return None;
    }
    let mantissa: f64 = parts[0].parse().ok()?;
    // Right side has the form `10<exponent>` (whitespace possible
    // between `10` and the exponent because the regex eats that).
    let right = parts[1].trim_start_matches("10").trim();
    let exp: i32 = right.parse().ok()?;
    Some(mantissa * 10f64.powi(exp))
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent::types::{ClaimKind, NumberRef, PolicyVerdict};

    fn cfg() -> CrosscheckConfig {
        CrosscheckConfig::default()
    }

    fn mk_claim(headline: &str, body: &str, support: Vec<(String, f64)>) -> Claim {
        Claim {
            id: "test".into(),
            session_id: "sess".into(),
            kind: ClaimKind::Profile,
            headline: headline.into(),
            body_markdown: body.into(),
            provenance: vec![],
            support_numbers: support
                .into_iter()
                .map(|(metric, value)| NumberRef { metric, value })
                .collect(),
            subgraph_slice: None,
            policy_verdict: PolicyVerdict::Approved,
            stubs_active: vec![],
            emitted_at_ms: 0,
        }
    }

    // --- Extractor pattern coverage -----------------------------------------

    #[test]
    fn plain_integer() {
        let out = extract_from_text("the wallet moved 302999700 lamports inbound");
        assert!(!out.is_empty());
        // 302,999,700 lamports = 0.3029997 SOL after canonicalization
        let n = &out[0];
        assert!(matches!(n.unit_class, UnitClass::Sol));
        assert!((n.value - 0.3029997).abs() < 1e-6);
    }

    #[test]
    fn comma_separated_sol() {
        let out = extract_from_text("12,300 SOL volume");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].value, 12300.0);
        assert!(matches!(out[0].unit_class, UnitClass::Sol));
    }

    #[test]
    fn k_suffix_sol() {
        let out = extract_from_text("about 5.1k SOL inflow");
        assert_eq!(out.len(), 1);
        assert!((out[0].value - 5100.0).abs() < 1e-6);
        assert!(matches!(out[0].unit_class, UnitClass::Sol));
        assert!(out[0].hedged);
    }

    #[test]
    fn m_b_suffix_raw() {
        let out = extract_from_text("1.2M tx and 1.5B operations");
        assert!(out.iter().any(|n| (n.value - 1.2e6).abs() < 1e-3));
        assert!(out.iter().any(|n| (n.value - 1.5e9).abs() < 1e-3));
    }

    #[test]
    fn word_trillion_lamports() {
        let out = extract_from_text("12 trillion lamports total volume");
        assert!(!out.is_empty());
        // 12e12 lamports = 12,000 SOL
        let n = &out[0];
        assert!((n.value - 12000.0).abs() < 1e-3);
        assert!(matches!(n.unit_class, UnitClass::Sol));
    }

    #[test]
    fn e_notation() {
        let out = extract_from_text("about 1.5e9 lamports inbound");
        assert!(!out.is_empty());
        let n = &out[0];
        // 1.5e9 lamports = 1.5 SOL
        assert!((n.value - 1.5).abs() < 1e-6);
    }

    #[test]
    fn scientific_x10_superscript() {
        let out = extract_from_text("about 1.2×10¹³ lamports");
        assert!(!out.is_empty());
        // 1.2e13 lamports = 12,000 SOL
        let n = out
            .iter()
            .find(|n| matches!(n.unit_class, UnitClass::Sol))
            .expect("should find SOL value");
        assert!((n.value - 12000.0).abs() < 1.0);
    }

    #[test]
    fn hedge_marker_widens_tolerance() {
        let claim = mk_claim("", "5,123 SOL volume", vec![]);
        // Bare "about 5k" = 5000, claim has 5123. ±15% of 5123 ≈ 768.
        // |5000 - 5123| = 123, well within 15%. Approve.
        assert!(cross_check("about 5k SOL inflow", &[claim], &[], cfg()).is_ok());
    }

    #[test]
    fn declarative_tolerance_tighter() {
        let claim = mk_claim("", "5,000 SOL volume", vec![]);
        // Declarative "5,800 SOL" vs claim 5,000. ±10% of 5000 = 500.
        // |5800 - 5000| = 800, exceeds 10%. Should retract.
        let res = cross_check("5,800 SOL was sent", &[claim], &[], cfg());
        assert!(res.is_err(), "expected retract, got {:?}", res);
    }

    #[test]
    fn community_id_match() {
        let claim = mk_claim("", "Wallet X is in community 42", vec![]);
        assert!(cross_check("placed in community 42 of the live graph", &[claim], &[], cfg()).is_ok());
    }

    #[test]
    fn community_id_mismatch_retracts() {
        let claim = mk_claim("", "Wallet X is in community 42", vec![]);
        let res = cross_check(
            "the wallet belongs to community 1977 inside the live graph",
            &[claim],
            &[],
            cfg(),
        );
        assert!(res.is_err(), "expected retract, got {:?}", res);
    }

    #[test]
    fn unsourced_number_retracts() {
        let claim = mk_claim("", "Wallet X moved 12,300 SOL inbound", vec![]);
        // Narrative invents 50,000.
        let res = cross_check("X moved roughly 50,000 SOL", &[claim], &[], cfg());
        assert!(res.is_err(), "expected retract, got {:?}", res);
    }

    #[test]
    fn no_numbers_passes() {
        let claim = mk_claim("", "anything", vec![]);
        assert!(cross_check("looks like a hub but no numbers", &[claim], &[], cfg()).is_ok());
    }

    #[test]
    fn unit_class_separation() {
        // 12,300 in narrative refers to connections (Count); claim
        // has 12,300 SOL. Should NOT match because unit classes differ.
        let claim = mk_claim("", "Wallet X moved 12,300 SOL inbound", vec![]);
        let res = cross_check("the wallet has 12,300 connections", &[claim], &[], cfg());
        assert!(
            res.is_err(),
            "expected retract on unit-class mismatch, got {:?}",
            res
        );
    }

    #[test]
    fn structured_support_numbers_are_source() {
        // Body has no numbers; support_numbers carry the truth.
        let claim = mk_claim("", "see counts", vec![("connections".into(), 73.0)]);
        assert!(cross_check("about 73 connections in the window", &[claim], &[], cfg()).is_ok());
    }

    #[test]
    fn multi_claim_lenient_set() {
        // Two claims; narrative cites a number from the second.
        let c1 = mk_claim("", "5,000 SOL inflow", vec![]);
        let c2 = mk_claim("", "Wallet X has 73 connections", vec![]);
        assert!(cross_check("about 73 connections", &[c1, c2], &[], cfg()).is_ok());
    }

    #[test]
    fn small_bare_integer_skipped() {
        // "Wallet X is a hub with 3 of these characteristics": 3 is too
        // small / context-free to audit. Should not retract.
        let claim = mk_claim("", "Wallet X has 73 connections", vec![]);
        assert!(cross_check("X has 3 distinguishing properties", &[claim], &[], cfg()).is_ok());
    }

    #[test]
    fn extra_source_satisfies_narrative_when_claims_dont() {
        // Ship 3: a narrative number can be sourced from primitive
        // bindings even when the model didn't restate the value
        // inside a Claim's prose. Here the claim has no numeric
        // content, but the binding-source carries `73 connections`,
        // so the cross-check approves.
        let claim = mk_claim("looks like a hub", "no numbers in body", vec![]);
        let extras = vec![ExtractedNumber {
            value: 73.0,
            unit_class: UnitClass::Count,
            hedged: false,
        }];
        let res = cross_check("about 73 connections", &[claim], &extras, cfg());
        assert!(res.is_ok(), "expected approve via extras, got {:?}", res);
    }

    #[test]
    fn digits_inside_wallet_address_not_classified_as_sol() {
        // Regression: the digit `3` inside an address like
        // `fueL3hBZj...` used to pick up a far-downstream "SOL"
        // mention via tail-extending past the address through the
        // rest of the sentence. The immediate-token classification
        // rule (ship 2.6.1) keeps the digit at unit_class=Raw, then
        // small_bare_integer_skipped drops it. Result: no false
        // retract on prose that mentions a wallet address AND any
        // number the model accidentally embedded a digit before.
        let claim = mk_claim(
            "",
            "Wallet X has 73 connections and zero SOL movement",
            vec![],
        );
        // Address-laden narrative that previously retracted with
        // "narrative number 3 SOL not found in cited Claims".
        let narr =
            "fueL3hBZjLLLJHiFH9cqZoozTG3XQZ53diwFPwbzNim is acting as a token-mint authority. \
             It has zero SOL movement but a high SPL degree (73) consistent with token mint behavior.";
        let res = cross_check(narr, &[claim], &[], cfg());
        assert!(
            res.is_ok(),
            "address-digit Sol-poisoning regressed; got {:?}",
            res
        );
    }
}
