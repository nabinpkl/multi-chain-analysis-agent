//! Dump byte-equivalent SSE goldens for every `SseFrame` variant.
//!
//! Phase I.5: Phase II onward uses these as byte-diff oracles when
//! the Python agent service emits frames. Drift between Rust and
//! Python wire formats surfaces as a failed golden parse / diff.
//!
//! Strategy: rather than running the live agent (which would need
//! ClickHouse + RPC + a real OpenRouter key + a populated graph),
//! we construct realistic `SseFrame` instances by hand and serialize
//! them through the same logic `api::agent::frame_to_event` uses. The
//! goldens lock the WIRE FORMAT, not specific dynamic values; the
//! ids and timestamps inside payloads are fixed seeds so the output
//! is deterministic across runs.
//!
//! `frame_to_event` itself is private to `api::agent`. We mirror its
//! match statement here. If `frame_to_event` ever changes shape, this
//! bin must change with it; the Phase I.5 tests on the Python side
//! fail loudly when that happens.
//!
//! Output: one file per scenario at
//! `agent-service/tests/goldens/<scenario>.sse`, exactly the bytes a
//! browser EventSource would see arrive on the wire (event-name +
//! data line + double-newline framing).
//!
//! Run via `cargo run --quiet --bin dump_sse_goldens` from the
//! `backend/` directory, or `just regen-sse-goldens` from the repo
//! root.

use std::fs;
use std::path::PathBuf;

use anyhow::{Context, Result};
use serde_json::json;

use multichain_engine::agent::SseFrame;
use multichain_engine::agent::types::{
    AgentDone, ChangedSince, Claim, ClaimKind, Delta, FieldChange, FieldDelta, GatePath, NoMovement,
    NumberRef, PathState, PathStep, PolicyVerdict, ProvenanceRef, AgentSwitches,
    NarrativeWithRefs,
};

// ---------------------------------------------------------------------------
// Inline mirror of `api::agent::frame_to_event`. Match it byte-for-byte.
// ---------------------------------------------------------------------------

/// Format a single frame as an SSE event block: `event: <name>\ndata: <json>\n\n`.
/// Mirror of the match in `api::agent::frame_to_event` plus
/// `done_to_event`. Keep them in sync.
fn frame_to_sse_bytes(frame: &SseFrame) -> String {
    let (event_name, data_json) = match frame {
        SseFrame::Claim(claim) => {
            ("Claim", serde_json::to_string(claim).expect("serialize Claim"))
        }
        SseFrame::Progress { phase, detail } => (
            "Progress",
            serde_json::to_string(&json!({"phase": phase, "detail": detail}))
                .expect("serialize Progress"),
        ),
        SseFrame::Narrative(payload) => (
            "Narrative",
            serde_json::to_string(payload).expect("serialize Narrative"),
        ),
        SseFrame::NarrativeRetracted {
            text,
            reason,
            debug_reason,
        } => {
            let mut payload = json!({"text": text, "reason": reason});
            if let Some(d) = debug_reason {
                payload["debug_reason"] = serde_json::Value::String(d.clone());
            }
            (
                "NarrativeRetracted",
                serde_json::to_string(&payload).expect("serialize NarrativeRetracted"),
            )
        }
        SseFrame::Error {
            message,
            debug_message,
        } => {
            let mut payload = json!({"message": message});
            if let Some(d) = debug_message {
                payload["debug_message"] = serde_json::Value::String(d.clone());
            }
            (
                "Error",
                serde_json::to_string(&payload).expect("serialize Error"),
            )
        }
        SseFrame::GatePath(path) => (
            "GatePath",
            serde_json::to_string(path).expect("serialize GatePath"),
        ),
        SseFrame::NoMovement(payload) => (
            "NoMovement",
            serde_json::to_string(payload).expect("serialize NoMovement"),
        ),
        SseFrame::ChangedSince(payload) => (
            "ChangedSince",
            serde_json::to_string(payload).expect("serialize ChangedSince"),
        ),
    };
    format!("event: {event_name}\ndata: {data_json}\n\n")
}

fn agent_done_to_sse_bytes(done: &AgentDone) -> String {
    let json = serde_json::to_string(done).expect("serialize AgentDone");
    format!("event: Done\ndata: {json}\n\n")
}

// ---------------------------------------------------------------------------
// Scenarios. Each builds a deterministic byte sequence representing one
// turn's worth of SSE output the frontend would see.
// ---------------------------------------------------------------------------

fn fixed_session_id() -> String {
    // Deterministic across runs. Real session ids are 16 random bytes
    // hex; we use a recognizable placeholder so the goldens are easy
    // to grep.
    "0000000000000000000000000000ffff".to_string()
}

fn fixed_claim_id() -> String {
    // Real claim ids are ULIDs. We use a fixed ULID-shaped placeholder
    // so anyone reading the goldens recognizes the slot.
    "01HKQ0000000000000000FIX01".to_string()
}

fn sample_provenance() -> Vec<ProvenanceRef> {
    vec![
        ProvenanceRef::Wallet {
            addr: "DLZSeiq2xjikgwcniQB6B89uodkbQHrTcco6mJu9UNuK".into(),
            idx: Some(0),
        },
        ProvenanceRef::Community { id: 8 },
        ProvenanceRef::Number {
            metric: "total_volume_lamports".into(),
            value: 80_223_943_444.0,
            support: vec![
                "DLZSeiq2xjikgwcniQB6B89uodkbQHrTcco6mJu9UNuK".into(),
            ],
        },
    ]
}

fn happy_path_wallet_profile() -> String {
    let claim = Claim {
        id: fixed_claim_id(),
        session_id: fixed_session_id(),
        kind: ClaimKind::Profile,
        headline: "Whale wallet ${ref:0} dominates community ${ref:1}".into(),
        body_markdown: "Wallet ${ref:0} moved ${ref:2} lamports in the live window.".into(),
        provenance: sample_provenance(),
        support_numbers: vec![NumberRef {
            metric: "total_volume_lamports".into(),
            value: 80_223_943_444.0,
        }],
        subgraph_slice: None,
        policy_verdict: PolicyVerdict::Approved,
        stubs_active: vec![],
        emitted_at_ms: 1234,
    };
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::Progress {
        phase: "planning".into(),
        detail: "reading context, choosing primitive".into(),
    }));
    buf.push_str(&frame_to_sse_bytes(&SseFrame::Claim(claim)));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn happy_path_emit_claim_with_narrative() -> String {
    let claim = Claim {
        id: fixed_claim_id(),
        session_id: fixed_session_id(),
        kind: ClaimKind::Profile,
        headline: "Wallet ${ref:0} is a whale".into(),
        body_markdown: "Volume ${ref:1} across ${ref:2} counterparties.".into(),
        provenance: sample_provenance(),
        support_numbers: vec![NumberRef {
            metric: "total_volume_lamports".into(),
            value: 80_223_943_444.0,
        }],
        subgraph_slice: None,
        policy_verdict: PolicyVerdict::Approved,
        stubs_active: vec![],
        emitted_at_ms: 1500,
    };
    let narrative = NarrativeWithRefs {
        text: "Wallet ${ref:0} is a heavy receiver in community ${ref:1} this minute.".into(),
        provenance: sample_provenance(),
    };
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::Claim(claim)));
    buf.push_str(&frame_to_sse_bytes(&SseFrame::Narrative(narrative)));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn narrative_retracted_by_constitution() -> String {
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::NarrativeRetracted {
        text: "I detected unusual MEV activity on this wallet.".into(),
        reason: "I could not verify that statement against the data I fetched.".into(),
        // debug_reason None: matches prod default (AGENT_DEBUG_PUBLIC unset)
        debug_reason: None,
    }));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn no_movement() -> String {
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::NoMovement(NoMovement {
        prior_turn: 1,
        primitives_replayed: vec!["wallet_profile".into()],
    })));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn changed_since() -> String {
    let delta = Delta {
        changed: vec![
            FieldDelta {
                field_path: "stats.in_volume_lamports".into(),
                primitive: "wallet_profile".into(),
                change: FieldChange::NumberMoved {
                    prior: 80_000_000_000.0,
                    current: 90_000_000_000.0,
                    pct: 0.125,
                },
            },
            FieldDelta {
                field_path: "top_counterparties".into(),
                primitive: "wallet_profile".into(),
                change: FieldChange::SetChanged {
                    added: vec!["NEW1111111111111111111111111111111111111111".into()],
                    removed: vec!["OLD2222222222222222222222222222222222222222".into()],
                },
            },
        ],
        unchanged_field_count: 4,
    };
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::ChangedSince(ChangedSince {
        prior_turn: 1,
        delta,
        prose: "Volume rose 12.5% since turn 1; one new counterparty appeared.".into(),
    })));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn error_terminal() -> String {
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::Error {
        message: "The agent could not complete this turn. Please try again.".into(),
        debug_message: None,
    }));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

fn gate_path_show_trace() -> String {
    let path = GatePath {
        channel: "narrative".into(),
        switches: AgentSwitches::default(),
        steps: vec![
            PathStep {
                stage: "narrative.stay_in_role".into(),
                state: PathState::Approved,
                elapsed_us: 1234,
                note: "constitution leg approved".into(),
            },
            PathStep {
                stage: "narrative.dont_fabricate".into(),
                state: PathState::Approved,
                elapsed_us: 567,
                note: "all narrative numbers found in binding store".into(),
            },
        ],
        final_verdict: PolicyVerdict::Approved,
    };
    let mut buf = String::new();
    buf.push_str(&frame_to_sse_bytes(&SseFrame::GatePath(path)));
    buf.push_str(&agent_done_to_sse_bytes(&AgentDone {
        session_id: fixed_session_id(),
        elapsed_ms: 4321,
    }));
    buf
}

// ---------------------------------------------------------------------------
// Driver: write each scenario to disk.
// ---------------------------------------------------------------------------

fn main() -> Result<()> {
    let out_dir = std::env::var("DUMP_GOLDENS_OUT_DIR")
        .unwrap_or_else(|_| "../agent-service/tests/goldens".to_string());
    let out_dir = PathBuf::from(out_dir);

    if out_dir.exists() {
        fs::remove_dir_all(&out_dir)
            .with_context(|| format!("clear {}", out_dir.display()))?;
    }
    fs::create_dir_all(&out_dir)
        .with_context(|| format!("create {}", out_dir.display()))?;

    println!("dumping SSE goldens to {}", out_dir.display());

    let scenarios: Vec<(&str, String)> = vec![
        ("happy_path_wallet_profile", happy_path_wallet_profile()),
        (
            "happy_path_emit_claim_with_narrative",
            happy_path_emit_claim_with_narrative(),
        ),
        (
            "narrative_retracted_by_constitution",
            narrative_retracted_by_constitution(),
        ),
        ("no_movement", no_movement()),
        ("changed_since", changed_since()),
        ("error_terminal", error_terminal()),
        ("gate_path_show_trace", gate_path_show_trace()),
    ];

    for (name, body) in scenarios {
        let path = out_dir.join(format!("{name}.sse"));
        fs::write(&path, body).with_context(|| format!("write {}", path.display()))?;
        println!("wrote {}", path.display());
    }

    println!("done");
    Ok(())
}
