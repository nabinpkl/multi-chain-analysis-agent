//! Versioned constitution for the output-policy gate (phase 03 layer
//! 3). Updating the constitution is a deliberate code change: bump the
//! tag, write a new `policy_prompt_vN.txt`, and let ship 6's eval suite
//! gate the change. Every session writes a `Prompt` ledger event with
//! `version_tag` and content hash so replay is exact.
//!
//! Pairs with `prompt.rs` (the primary system prompt). The two files
//! must stay aligned: rules in this constitution are what the gate
//! enforces; the primary's prompt should make the model compliant by
//! its own lights so the gate has nothing to retract on most turns.
//! Drift between the two = primary that's compliant by its prompt's
//! rules but the gate retracts anyway, or vice versa.

pub const POLICY_PROMPT_V1_TAG: &str = "policy_v1";

/// Ship 2 constitution. Six rules: provenance, no-imperative-leak,
/// domain, identity, narrative-hedging, no-identity-guessing.
/// Superseded by v2 in ship 2.5; kept compiled-in for ledger replay
/// of pre-2.5 sessions.
pub const POLICY_PROMPT_V1_TEXT: &str = include_str!("policy_prompt_v1.txt");

pub const POLICY_PROMPT_V2_TAG: &str = "policy_v2";

/// Ship 2.5 constitution. Same six-rule structure as v1, but Rule 5
/// is rewritten from "hedging" to "no calculation" because the
/// deterministic numerical cross-check (see `policy_crosscheck.rs`)
/// now enforces the result deterministically. Superseded by v3 in
/// ship 2.7; kept compiled-in for ledger replay of pre-2.7 sessions.
pub const POLICY_PROMPT_V2_TEXT: &str = include_str!("policy_prompt_v2.txt");

pub const POLICY_PROMPT_V3_TAG: &str = "policy_v3";

/// Ship 2.7 constitution. Same six-rule structure as v2, plus an
/// "Extraction sidecar" section instructing the LLM to also output
/// a structured list of every number it sees in narrative + cited
/// Claims, each with a `unit_class` tag. The runtime runs a
/// deterministic cross-check on the extracted sets in parallel
/// with the existing regex-based extractor; the three-verdict
/// merge (regex / llm-extraction / constitution) lives in
/// `policy.rs::OutputPolicy::check_narrative`. Superseded by v4 in
/// ship 5a; kept compiled-in for ledger replay.
pub const POLICY_PROMPT_V3_TEXT: &str = include_str!("policy_prompt_v3.txt");

pub const POLICY_PROMPT_V4_TAG: &str = "policy_v4";

/// Ship 5a constitution. Rule 5 reframed from "no calculation in
/// narrative" to "citation discipline": every audit-class number in
/// narrative AND claim body_markdown MUST appear as `${ref:N}`
/// placeholder pointing at a provenance entry. Bare audit numbers
/// retract; bare descriptive numbers ("3 distinguishing properties",
/// "since 2024") approve. The deterministic structural gate
/// (`policy_placeholder` + `policy_structural`) handles index
/// resolution + value tracing; the constitution LLM judge handles
/// the audit-vs-descriptive classification on bare numbers (the
/// part that requires reading meaning). Extraction sidecar still
/// runs but its `paraphrase_aware_match` cross-check is now
/// advisory-only in the merge (the structural gate is load-bearing
/// for factuality). Six-rule structure preserved for stability;
/// only Rule 5 + the extraction sidecar's role-note changed.
pub const POLICY_PROMPT_V4_TEXT: &str = include_str!("policy_prompt_v4.txt");

/// Active constitution accessor. Adding a v5 means adding a const +
/// a match arm here; sessions can be replayed against the exact
/// constitution version their ledger references.
pub fn active_policy_prompt() -> (&'static str, &'static str) {
    (POLICY_PROMPT_V4_TAG, POLICY_PROMPT_V4_TEXT)
}
