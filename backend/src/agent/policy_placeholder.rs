//! Ship 5a `${ref:N}` placeholder parser + index validator.
//!
//! Single deterministic check the gate runs on Claim body_markdown and
//! Narrative text: every `${ref:N}` token must point at a valid index
//! in the surrounding provenance array. Out-of-bounds → retract.
//!
//! This is the ONLY job regex has on the backend after ship 5a. The
//! pattern (`\$\{ref:(\d+)\}`) targets a deterministic ASCII grammar
//! the model is instructed to emit; not interpreting prose, just
//! locating tokens. Unicode-safe: regex match positions are always
//! char-boundary, parsed digits are ASCII, no byte-arithmetic in the
//! hot path. The byte-slice panic class (ship 4 dogfood) cannot
//! recur here.
//!
//! Frontend uses the equivalent regex `/\$\{ref:(\d+)\}/g` in
//! `claim-cards/profile-card.tsx` to render chips. Same grammar,
//! same job, two callers.

use std::sync::LazyLock;

use regex::Regex;

/// `\$\{ref:(\d+)\}`. Captures the index as group 1. Anchored only
/// to the literal characters; any surrounding prose is ignored.
static REF_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\$\{ref:(\d+)\}").expect("REF_RE is a valid regex")
});

/// Reason a placeholder failed validation. The retract message
/// surfaces this on `NarrativeRetracted` / claim retraction so the
/// model's retry-feedback message can name the specific failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RefError {
    /// `${ref:N}` index N is >= provenance.len(). Carries N and the
    /// length so the retry message can name both.
    OutOfBounds { index: u32, provenance_len: usize },
    /// Index parsed but exceeded `u32::MAX` somehow (effectively
    /// impossible for any sensible provenance array, but we still
    /// guard against parse-failure paths).
    ParseFail { raw: String },
}

impl RefError {
    pub fn to_human_string(&self) -> String {
        match self {
            RefError::OutOfBounds {
                index,
                provenance_len,
            } => format!(
                "${{ref:{index}}} is out of bounds; provenance has {provenance_len} entr{}",
                if *provenance_len == 1 { "y" } else { "ies" },
            ),
            RefError::ParseFail { raw } => {
                format!("could not parse `${{ref:{raw}}}` as a u32 index")
            }
        }
    }
}

/// Walk `text` for every `${ref:N}` token, parse N, verify it's a
/// valid index into the surrounding provenance array. Returns the
/// first error encountered, or `Ok(())` if every ref resolves (or
/// the text contains no refs at all).
///
/// The "first error" semantics matches how the policy gate already
/// surfaces single retract reasons. If a turn has multiple bad refs
/// the model will see the first on retry and self-correct; subsequent
/// refs surface on the next retry attempt if still wrong. Cheap to
/// switch to "collect all errors" later if dogfood demands it.
pub fn validate_refs(text: &str, provenance_len: usize) -> Result<(), RefError> {
    for cap in REF_RE.captures_iter(text) {
        // cap[1] is the digit run inside `${ref:...}`. Always ASCII
        // (the regex's character class is `\d`), so parse can only
        // fail on overflow > u32::MAX. Defensive but realistic
        // provenance arrays never approach that.
        let raw = &cap[1];
        let n: u32 = raw.parse().map_err(|_| RefError::ParseFail {
            raw: raw.to_string(),
        })?;
        if (n as usize) >= provenance_len {
            return Err(RefError::OutOfBounds {
                index: n,
                provenance_len,
            });
        }
    }
    Ok(())
}

/// Convenience: count the placeholder tokens in the text without
/// validating indices. Useful for path-trace notes ("narrative
/// contained 3 chip references") and for the structural gate's
/// "no refs at all" branch (skip lookup entirely if text doesn't
/// cite anything).
pub fn count_refs(text: &str) -> usize {
    REF_RE.captures_iter(text).count()
}

/// Iterator over every parsed ref index in the text, in document
/// order. `validate_refs` calls this internally; exposed publicly
/// so callers can build per-ref structures (e.g., the structural
/// gate's "every Number ref has a binding match" check) without
/// re-parsing. Yields `Err(RefError::ParseFail)` on the rare digit-
/// overflow case (and short-circuits at first error).
pub fn iter_ref_indices(text: &str) -> impl Iterator<Item = Result<u32, RefError>> + '_ {
    REF_RE.captures_iter(text).map(|cap| {
        let raw = &cap[1];
        raw.parse::<u32>().map_err(|_| RefError::ParseFail {
            raw: raw.to_string(),
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_in_bounds() {
        let text = "Wallet ${ref:0} has ${ref:2} connections.";
        assert!(validate_refs(text, 3).is_ok());
    }

    #[test]
    fn rejects_out_of_bounds() {
        let text = "Wallet ${ref:5} has connections.";
        let err = validate_refs(text, 3).unwrap_err();
        assert_eq!(
            err,
            RefError::OutOfBounds {
                index: 5,
                provenance_len: 3,
            }
        );
    }

    #[test]
    fn rejects_first_out_of_bounds_when_multiple_refs() {
        // Two refs; first valid, second invalid. We surface the
        // first failure (in document order).
        let text = "${ref:0} then ${ref:99}";
        let err = validate_refs(text, 1).unwrap_err();
        match err {
            RefError::OutOfBounds { index, .. } => assert_eq!(index, 99),
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn empty_provenance_with_any_ref_retracts() {
        let text = "Wallet ${ref:0}.";
        let err = validate_refs(text, 0).unwrap_err();
        assert_eq!(
            err,
            RefError::OutOfBounds {
                index: 0,
                provenance_len: 0,
            }
        );
    }

    #[test]
    fn handles_no_refs_at_all() {
        // Pure prose without citations: descriptive narrative with
        // no audit data claims. Approve unconditionally.
        let text = "The wallet has 3 distinguishing properties.";
        assert!(validate_refs(text, 0).is_ok());
        assert!(validate_refs(text, 5).is_ok());
    }

    #[test]
    fn unicode_safe_apostrophe() {
        // Curly apostrophe is U+2019 (3 bytes in UTF-8). The byte-
        // slice panic from ship 4 dogfood lived in the old hedge
        // detector; this validator only inspects regex match
        // positions (always char-boundary) and ASCII digit captures.
        // Must not panic regardless of unicode in surrounding prose.
        let text = "I\u{2019}m looking at wallet ${ref:0}\u{2014}it has ${ref:1} connections.";
        assert!(validate_refs(text, 2).is_ok());
    }

    #[test]
    fn unicode_safe_em_dash() {
        let text = "Wallet\u{2014}${ref:0}\u{2014}has activity.";
        assert!(validate_refs(text, 1).is_ok());
    }

    #[test]
    fn count_refs_works_with_repeats() {
        // The same index referenced multiple times counts each
        // occurrence (we validate each occurrence independently).
        let text = "${ref:0} and ${ref:0} again, plus ${ref:1}.";
        assert_eq!(count_refs(text), 3);
    }

    #[test]
    fn count_refs_zero_when_no_refs() {
        assert_eq!(count_refs("plain prose"), 0);
        assert_eq!(count_refs(""), 0);
    }

    #[test]
    fn iter_ref_indices_yields_in_document_order() {
        let text = "${ref:2} first, ${ref:0} second, ${ref:1} third.";
        let got: Vec<u32> = iter_ref_indices(text)
            .map(|r| r.expect("digits parse"))
            .collect();
        assert_eq!(got, vec![2, 0, 1]);
    }

    #[test]
    fn malformed_token_is_skipped_not_validated() {
        // `${ref :0}`, `$(ref:0)`, `${REF:0}` etc. don't match the
        // grammar; they're skipped entirely. This is intentional:
        // the gate doesn't accept lenient variants. If the model
        // emits a malformed token the chip simply doesn't resolve,
        // the audit number appears as bare prose in the rendered
        // output, and the constitution gate (LLM judge) catches
        // it via the citation discipline rule. Dual safety net.
        let text = "Wallet $(ref:0) has activity, ${REF:1} somewhere.";
        // No matches → no validation work → Ok regardless of
        // provenance length.
        assert!(validate_refs(text, 0).is_ok());
        assert_eq!(count_refs(text), 0);
    }

    #[test]
    fn human_string_singular_vs_plural() {
        let s = RefError::OutOfBounds {
            index: 3,
            provenance_len: 1,
        }
        .to_human_string();
        assert!(s.contains("1 entry"), "{}", s);

        let s = RefError::OutOfBounds {
            index: 3,
            provenance_len: 4,
        }
        .to_human_string();
        assert!(s.contains("4 entries"), "{}", s);
    }
}
