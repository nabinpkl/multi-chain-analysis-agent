//! Session replay. Reads all events for a session in `(sequence)`
//! order. v0 has no caller in code; ship 6's eval suite consumes it.
//! Available now so the contract is settled and the file structure
//! does not change later.

use anyhow::{Context, Result};
use clickhouse::Client;

use super::event::{LedgerEvent, LedgerEventKind, LedgerRowRead, hex_decode};

pub async fn replay_session(client: &Client, session_id: &str) -> Result<Vec<LedgerEvent>> {
    let rows: Vec<LedgerRowRead> = client
        .query(
            "
            SELECT
                session_id, sequence, timestamp_ms, kind, principal_hash,
                payload, payload_hash, pre_estimate_units, post_actual_units,
                cost_relevant, redaction_policy_ver
            FROM multichain.agent_ledger
            WHERE session_id = ?
            ORDER BY sequence ASC
            ",
        )
        .bind(session_id)
        .fetch_all()
        .await
        .context("agent_ledger replay query")?;

    let events = rows
        .into_iter()
        .map(|r| LedgerEvent {
            session_id: r.session_id,
            sequence: r.sequence,
            timestamp_ms: r.timestamp_ms,
            kind: parse_kind(&r.kind),
            principal_hash: hex_decode(&r.principal_hash),
            payload: r.payload,
            payload_hash: hex_decode(&r.payload_hash),
            pre_estimate_units: r.pre_estimate_units,
            post_actual_units: r.post_actual_units,
            cost_relevant: r.cost_relevant != 0,
            redaction_policy_ver: r.redaction_policy_ver,
        })
        .collect();
    Ok(events)
}

fn parse_kind(s: &str) -> LedgerEventKind {
    match s {
        "session_started" => LedgerEventKind::SessionStarted,
        "prompt" => LedgerEventKind::Prompt,
        "llm_call" => LedgerEventKind::LlmCall,
        "llm_response" => LedgerEventKind::LlmResponse,
        "tool_call" => LedgerEventKind::ToolCall,
        "tool_result" => LedgerEventKind::ToolResult,
        "claim_emitted" => LedgerEventKind::ClaimEmitted,
        "policy_verdict" => LedgerEventKind::PolicyVerdict,
        "budget_decrement" => LedgerEventKind::BudgetDecrement,
        "session_ended" => LedgerEventKind::SessionEnded,
        // Unknown kinds shouldn't occur but we don't want replay to
        // panic. Map to SessionEnded as a deliberate eyebrow-raise.
        _ => LedgerEventKind::SessionEnded,
    }
}
