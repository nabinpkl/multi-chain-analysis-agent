//! Versioned system prompt. Updating the prompt is a deliberate code
//! change: bump the tag, write a new `prompt_vN.txt`, and let ship 6's
//! eval suite gate the change. Every session writes a `Prompt` ledger
//! event with `version_tag` and content hash so replay is exact.

pub const PROMPT_V1_TAG: &str = "system_v1";

/// Ship 1 prompt. Single output channel (`emit_claim` always called).
/// Kept compiled in so ledger replay of pre-1.6 sessions can resolve
/// their prompt content by tag without git archeology.
pub const PROMPT_V1_TEXT: &str = include_str!("prompt_v1.txt");

pub const PROMPT_V2_TAG: &str = "system_v2";

/// Ship 1.6 prompt. Two output channels: `emit_claim` (structured data
/// with provenance) and Narrative (free-form prose returned as the
/// final assistant text). Drops the rigid fetch-then-emit loop and
/// teaches follow-up etiquette: if the entity is already profiled in
/// the thread history, don't re-fetch; interpret instead. The
/// `narrative.no_factuality_gate` stub flags that prose numbers are
/// not yet cross-checked against cited Claims (ship 2 closes this).
pub const PROMPT_V2_TEXT: &str = include_str!("prompt_v2.txt");

/// Active prompt accessor. Adding a v3 means adding a const + a match
/// arm here; sessions can be replayed against the exact prompt
/// version their ledger references.
pub fn active_prompt() -> (&'static str, &'static str) {
    (PROMPT_V2_TAG, PROMPT_V2_TEXT)
}
