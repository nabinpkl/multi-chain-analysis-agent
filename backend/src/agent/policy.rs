//! Output-policy gate (phase 03 layer 3). Ship 2 promotes this from
//! the always-approve stub to a real cheap-model call against the
//! constitution in `policy_prompt_v1.txt`.
//!
//! Two entry points, one constitution:
//! - `check_claim(claim)`: fires for every `emit_claim` invocation.
//! - `check_narrative(text, same_turn_claims)`: fires for every
//!   non-empty assistant text the loop receives from rig.
//!
//! Both methods route through `AgentClient::complete_policy`, which
//! talks to the small `policy_model` (default
//! `openai/gpt-oss-20b:free`). The policy model returns a JSON
//! `{verdict, reason}` object; malformed output fails closed
//! (`Retracted { reason: "policy parse failure" }`) because a defense
//! layer should fail secure, not fail open.

use serde::Deserialize;
use tracing::{info, warn};

use super::client::AgentClient;
use super::policy_crosscheck::{cross_check, CrosscheckConfig};
use super::policy_prompt::{POLICY_PROMPT_V2_TAG, POLICY_PROMPT_V2_TEXT};
use super::types::{Claim, PolicyVerdict};

/// Per-channel context the gate sees. Used to build the user-message
/// JSON sent to the cheap model.
#[derive(serde::Serialize)]
struct GateRequest<'a, T: serde::Serialize> {
    channel: &'static str,
    payload: &'a T,
    #[serde(skip_serializing_if = "<[Claim]>::is_empty")]
    same_turn_claims: &'a [Claim],
}

#[derive(Deserialize)]
struct GateResponse {
    verdict: String,
    #[serde(default)]
    reason: String,
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
                tag = POLICY_PROMPT_V2_TAG,
                len = POLICY_PROMPT_V2_TEXT.len(),
                policy_model = c.policy_model(),
                "policy gate online (constitution v2)",
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
            constitution_tag: POLICY_PROMPT_V2_TAG,
            constitution_text: POLICY_PROMPT_V2_TEXT,
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
    pub async fn check_claim(&self, claim: &Claim) -> PolicyVerdict {
        let req = GateRequest {
            channel: "claim",
            payload: claim,
            same_turn_claims: &[],
        };
        let user = match serde_json::to_string(&req) {
            Ok(s) => s,
            Err(e) => {
                warn!(error = %e, "policy: serialize claim payload failed");
                return PolicyVerdict::Retracted {
                    reason: "policy serialize failure".into(),
                };
            }
        };
        self.run_gate("claim", &user).await
    }

    /// Verdict for a Narrative emission.
    ///
    /// Two layers, in order:
    /// 1. **Deterministic numerical cross-check** (ship 2.5). Every
    ///    number in `text` must match (within tolerance) at least one
    ///    number extracted from `same_turn_claims` ∪
    ///    `thread_history_claims` of the same unit class. Fails fast:
    ///    if the cross-check retracts, we skip the cheap-model gate
    ///    entirely (saves the 2-5s round trip).
    /// 2. **Cheap-model constitution gate** (ship 2). Rules 1-6 from
    ///    `policy_prompt_v2.txt`. Catches off-domain, imperative-leak,
    ///    identity drift, identity guessing, etc.  the things code
    ///    can't audit.
    ///
    /// `same_turn_claims` is what the agent cited THIS turn;
    /// `thread_history_claims` is what it cited in earlier turns of
    /// the same thread (lenient mode for follow-up turns that
    /// legitimately restate prior numbers).
    pub async fn check_narrative(
        &self,
        text: &str,
        same_turn_claims: &[Claim],
        thread_history_claims: &[Claim],
    ) -> PolicyVerdict {
        // Stage 1: deterministic cross-check pre-flight.
        let mut all_claims: Vec<Claim> =
            Vec::with_capacity(same_turn_claims.len() + thread_history_claims.len());
        all_claims.extend(same_turn_claims.iter().cloned());
        all_claims.extend(thread_history_claims.iter().cloned());
        if let Err(reason) = cross_check(text, &all_claims, CrosscheckConfig::default()) {
            let human = reason.to_human_string();
            info!(
                reason = %human,
                same_turn = same_turn_claims.len(),
                thread_history = thread_history_claims.len(),
                "narrative cross-check retracted",
            );
            return PolicyVerdict::Retracted { reason: human };
        }

        // Stage 2: cheap-model constitution gate (existing ship 2).
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
                return PolicyVerdict::Retracted {
                    reason: "policy serialize failure".into(),
                };
            }
        };
        self.run_gate("narrative", &user).await
    }

    /// Shared call site: hands the constitution + payload to the cheap
    /// model, parses the response, returns a verdict. Fail-closed on
    /// anything weird.
    async fn run_gate(&self, channel: &str, user: &str) -> PolicyVerdict {
        let client = match &self.client {
            Some(c) => c,
            None => {
                // No client = agent disabled. Auto-approve so the
                // (unreachable from real endpoints) call site doesn't
                // stall. The boot-time warn already named this.
                return PolicyVerdict::Approved;
            }
        };
        let raw = match client
            .complete_policy(self.constitution_text, user)
            .await
        {
            Ok(s) => s,
            Err(e) => {
                warn!(channel, error = %e, "policy: cheap-model call failed");
                return PolicyVerdict::Retracted {
                    reason: "policy gate unavailable".into(),
                };
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
                    "policy: response did not parse as {{verdict, reason}}",
                );
                return PolicyVerdict::Retracted {
                    reason: "policy parse failure".into(),
                };
            }
        };

        match resp.verdict.trim().to_ascii_lowercase().as_str() {
            "approve" | "approved" => PolicyVerdict::Approved,
            "retract" | "retracted" | "reject" | "rejected" => {
                let reason = if resp.reason.trim().is_empty() {
                    "constitution violation".to_string()
                } else {
                    resp.reason
                };
                PolicyVerdict::Retracted { reason }
            }
            other => {
                warn!(
                    channel,
                    verdict = %other,
                    "policy: unknown verdict; failing closed",
                );
                PolicyVerdict::Retracted {
                    reason: "policy unknown verdict".into(),
                }
            }
        }
    }

    // `hit_narrative_stub` retired in ship 2.5 alongside the
    // `narrative.no_numerical_crosscheck` stub. Cross-check landed
    // and the remaining narrative-gating concerns are inside
    // `check_narrative` itself; no stub to mark anymore.
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
