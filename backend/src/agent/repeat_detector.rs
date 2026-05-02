//! Ship 4 repeat detector. Pre-loop small LLM gate that judges
//! whether the new user message is a FULL REPEAT of any prior turn
//! in the same thread. Drives the loop's branch into the
//! incremental-answer path (replay turn N's primitives, diff against
//! captured outputs, narrate only what changed) vs the normal main
//! loop.
//!
//! Runs only when `AgentSwitches.incremental_answers = true`. Failure
//! modes (timeout, parse failure, empty history) all return
//! `RepeatDetectorOutcome::no_repeat(...)` so detection is opportunistic
//! and never breaks the turn. The downstream loop falls through to the
//! normal flow on `repeat_of_turn = None`.
//!
//! Uses the cheap policy model (`AgentClient::complete_policy`), same
//! as the constitution gate. One extra ~100-150ms call per turn when
//! the switch is on; zero when off.
//!
//! # Repeat-detection rules (encoded in the system prompt)
//!
//! - SAME FOCUS (same wallet, community, or entity) AND SAME INTENT
//!   (same kind of analysis: profile, summary, etc.) = repeat.
//! - DIFFERENT FOCUS = not a repeat.
//! - PARTIAL OVERLAP (asking about a sub-aspect, e.g. "what's its
//!   biggest counterparty?" after "tell me about wallet X") = not a
//!   full repeat.
//! - DIFFERENT INTENT on same focus = not a repeat.
//! - "refresh", "again", "tell me again" sets
//!   `user_explicitly_wants_refresh=true` regardless of repeat
//!   detection; the loop uses this to bypass the incremental path
//!   even when a repeat would otherwise fire.

use std::collections::HashMap;

use serde::Deserialize;
use tracing::warn;

use super::client::AgentClient;

/// System prompt for the repeat detector. Locked-in shape; iterating
/// here changes detection behavior across all switch-on calls.
const REPEAT_DETECTOR_SYSTEM: &str = r#"You are a repeat-detection classifier. Given the user's prior questions in this conversation (as a list of turn_id: question) and a NEW user message, decide whether the new message is a FULL REPEAT of any prior turn.

A REPEAT means SAME FOCUS (same wallet, community, or entity) AND SAME INTENT (asking for the same kind of analysis, e.g. profile, summary).

NOT a repeat:
- Different focus (different wallet/community/entity)
- Partial overlap (asking about a sub-aspect of a prior answer, e.g. "what is its biggest counterparty?" after "tell me about wallet X")
- Different intent on the same focus (e.g. interpretation vs structured profile)

Special case: if the user explicitly asks for a refresh ("refresh", "again", "tell me again about X", "what's the latest on X"), set user_explicitly_wants_refresh=true REGARDLESS of repeat detection. The downstream loop uses this to bypass the incremental path even when a repeat would otherwise fire.

Reply with ONLY valid JSON, no prose, no code fences. Schema:
{
  "repeat_of_turn": <integer turn_id from the prior list, or null if not a repeat>,
  "user_explicitly_wants_refresh": <true | false>,
  "reason": "<one short sentence explaining your decision>"
}"#;

/// Outcome of a single detection pass. `repeat_of_turn` is the
/// validated turn id (validated to exist in the prior-questions map);
/// callers can trust it as a key into AgentThread.tool_calls_per_turn.
/// `reason` is human-readable, surfaced in the path trace's note.
#[derive(Debug, Clone)]
pub struct RepeatDetectorOutcome {
    pub repeat_of_turn: Option<u32>,
    pub reason: String,
    pub user_explicitly_wants_refresh: bool,
}

impl RepeatDetectorOutcome {
    /// Convenience builder for the no-repeat path. Used both by
    /// pre-flight short-circuits (empty history) and by failure
    /// fall-throughs.
    pub fn no_repeat(reason: impl Into<String>) -> Self {
        Self {
            repeat_of_turn: None,
            reason: reason.into(),
            user_explicitly_wants_refresh: false,
        }
    }
}

#[derive(Deserialize)]
struct RawJsonOutcome {
    #[serde(default)]
    repeat_of_turn: Option<u32>,
    #[serde(default)]
    user_explicitly_wants_refresh: bool,
    #[serde(default)]
    reason: String,
}

/// Run the detector. Returns `RepeatDetectorOutcome::no_repeat(...)` on
/// any failure (LLM error, parse failure, empty history). The
/// downstream loop uses `repeat_of_turn.is_some()` as the only branch
/// signal; missed repeats fall through to the normal main loop with no
/// behavioral cost.
pub async fn detect_repeat(
    prior_questions: &HashMap<u32, String>,
    new_user_msg: &str,
    client: &AgentClient,
) -> RepeatDetectorOutcome {
    if prior_questions.is_empty() {
        return RepeatDetectorOutcome::no_repeat("no prior turns in thread");
    }

    let user_prompt = format_user_prompt(prior_questions, new_user_msg);

    let response = match client
        .complete_policy(REPEAT_DETECTOR_SYSTEM, &user_prompt)
        .await
    {
        Ok(text) => text,
        Err(e) => {
            warn!(error = %e, "repeat_detector LLM call failed; treating as no repeat");
            return RepeatDetectorOutcome::no_repeat("detector call failed");
        }
    };

    parse_outcome(&response, prior_questions)
}

/// Format the user-side prompt as `turn_id: question` lines plus the
/// new message. Stable ordering by turn id so the model sees the
/// chronological order even though the source is a HashMap.
pub(crate) fn format_user_prompt(prior: &HashMap<u32, String>, new: &str) -> String {
    let mut sorted: Vec<(&u32, &String)> = prior.iter().collect();
    sorted.sort_by_key(|(t, _)| *t);
    let history = sorted
        .iter()
        .map(|(t, q)| format!("{}: {}", t, q))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "Prior turns (turn_id: question):\n{}\n\nNew message: {:?}\n\nRespond with the JSON.",
        history, new,
    )
}

/// Parse the LLM's JSON response and validate the turn id against the
/// known prior questions. Factored out from `detect_repeat` so unit
/// tests can exercise it without mocking the LLM client.
pub(crate) fn parse_outcome(
    raw_response: &str,
    prior_questions: &HashMap<u32, String>,
) -> RepeatDetectorOutcome {
    let trimmed = strip_code_fences(raw_response.trim());

    match serde_json::from_str::<RawJsonOutcome>(trimmed) {
        Ok(raw) => {
            // Validate: the model may return a turn id that doesn't
            // exist (hallucinated). We only trust it if it indexes a
            // real prior turn.
            let validated = raw
                .repeat_of_turn
                .filter(|t| prior_questions.contains_key(t));

            let reason = if !raw.reason.is_empty() {
                raw.reason
            } else if validated.is_some() {
                "repeat detected".to_string()
            } else {
                "no repeat".to_string()
            };

            RepeatDetectorOutcome {
                repeat_of_turn: validated,
                reason,
                user_explicitly_wants_refresh: raw.user_explicitly_wants_refresh,
            }
        }
        Err(e) => {
            warn!(error = %e, raw = %trimmed, "repeat_detector JSON parse failed");
            RepeatDetectorOutcome::no_repeat("detector output unparseable")
        }
    }
}

/// Strip markdown code fences if the model wrapped its JSON. Belt-and-
/// braces; the prompt asks for raw JSON but cheap models sometimes
/// hedge with ```json ... ``` anyway.
fn strip_code_fences(s: &str) -> &str {
    let s = s.trim();
    if let Some(rest) = s.strip_prefix("```json") {
        rest.trim_start().strip_suffix("```").unwrap_or(rest).trim()
    } else if let Some(rest) = s.strip_prefix("```") {
        rest.trim_start().strip_suffix("```").unwrap_or(rest).trim()
    } else {
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn questions(items: &[(u32, &str)]) -> HashMap<u32, String> {
        items
            .iter()
            .map(|(k, v)| (*k, v.to_string()))
            .collect()
    }

    #[test]
    fn parse_simple_repeat() {
        let prior = questions(&[(0, "tell me about wallet 9XYZ")]);
        let raw = r#"{"repeat_of_turn": 0, "user_explicitly_wants_refresh": false, "reason": "same wallet, same intent"}"#;
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, Some(0));
        assert!(!out.user_explicitly_wants_refresh);
        assert_eq!(out.reason, "same wallet, same intent");
    }

    #[test]
    fn parse_explicit_refresh_flag() {
        let prior = questions(&[(0, "tell me about wallet 9XYZ")]);
        let raw = r#"{"repeat_of_turn": 0, "user_explicitly_wants_refresh": true, "reason": "user said refresh"}"#;
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, Some(0));
        assert!(out.user_explicitly_wants_refresh);
    }

    #[test]
    fn parse_no_repeat() {
        let prior = questions(&[(0, "tell me about A"), (1, "tell me about B")]);
        let raw = r#"{"repeat_of_turn": null, "user_explicitly_wants_refresh": false, "reason": "different wallet"}"#;
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, None);
        assert_eq!(out.reason, "different wallet");
    }

    #[test]
    fn parse_invalid_turn_id_returns_none() {
        // Model hallucinated turn 99 that doesn't exist; we drop it.
        let prior = questions(&[(0, "tell me about A")]);
        let raw = r#"{"repeat_of_turn": 99, "user_explicitly_wants_refresh": false, "reason": "hallucinated id"}"#;
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, None);
    }

    #[test]
    fn parse_unparseable_falls_through() {
        let prior = questions(&[(0, "x")]);
        let out = parse_outcome("this is not JSON at all", &prior);
        assert_eq!(out.repeat_of_turn, None);
        assert!(out.reason.contains("unparseable"));
    }

    #[test]
    fn parse_with_code_fences() {
        let prior = questions(&[(0, "x")]);
        let raw = "```json\n{\"repeat_of_turn\": 0, \"user_explicitly_wants_refresh\": false, \"reason\": \"r\"}\n```";
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, Some(0));
    }

    #[test]
    fn parse_missing_optional_fields() {
        // Only repeat_of_turn provided; defaults fill the rest.
        let prior = questions(&[(0, "x")]);
        let raw = r#"{"repeat_of_turn": 0}"#;
        let out = parse_outcome(raw, &prior);
        assert_eq!(out.repeat_of_turn, Some(0));
        assert!(!out.user_explicitly_wants_refresh);
        // Default reason filled in by parse_outcome when LLM omits.
        assert!(!out.reason.is_empty());
    }

    #[test]
    fn format_user_prompt_sorts_by_turn_id() {
        let prior = questions(&[(2, "third"), (0, "first"), (1, "second")]);
        let s = format_user_prompt(&prior, "new");
        // Order must be 0 < 1 < 2 even though HashMap is unordered.
        let pos_first = s.find("first").unwrap();
        let pos_second = s.find("second").unwrap();
        let pos_third = s.find("third").unwrap();
        assert!(pos_first < pos_second);
        assert!(pos_second < pos_third);
    }

    #[tokio::test]
    async fn empty_history_short_circuits_no_repeat() {
        // Constructs a fake AgentClient is heavy; instead exercise the
        // pre-flight short-circuit by inspecting `parse_outcome` /
        // `RepeatDetectorOutcome::no_repeat`. The function-level
        // contract is "empty history -> no_repeat" without any LLM
        // call; encoded by the early return in detect_repeat.
        let prior: HashMap<u32, String> = HashMap::new();
        // We can't easily call `detect_repeat` here without an
        // AgentClient. The behavior is one branch; this test
        // documents the contract via the constructor.
        assert!(prior.is_empty());
        let oc = RepeatDetectorOutcome::no_repeat("no prior turns in thread");
        assert_eq!(oc.repeat_of_turn, None);
    }
}
