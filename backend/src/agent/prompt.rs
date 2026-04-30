//! Versioned system prompt. Updating the prompt is a deliberate code
//! change: bump the tag, write a new `prompt_vN.txt`, and let ship 6's
//! eval suite gate the change. Every session writes a `Prompt` ledger
//! event with `version_tag` and content hash so replay is exact.

pub const PROMPT_V1_TAG: &str = "system_v1";

/// Static system prompt content. Loaded at compile time so the binary
/// is self-contained. The text covers identity, the `<context>` rule,
/// the `<external_data>` rule, the provenance contract, the temporal
/// frame default per D-6, the output-policy summary, and cost-aware
/// behavior.
pub const PROMPT_V1_TEXT: &str = include_str!("prompt_v1.txt");

/// Active prompt accessor. Adding a v2 means adding a const + a match
/// arm here; sessions can be replayed against the exact prompt
/// version their ledger references.
pub fn active_prompt() -> (&'static str, &'static str) {
    (PROMPT_V1_TAG, PROMPT_V1_TEXT)
}
