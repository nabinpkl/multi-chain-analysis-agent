//! Ledger event types. All variants are written by the loop in v0.
//! `BudgetDecrement` rows have zero in cost columns until ship 4 fills
//! them. Phase 04 does not add events; it formalizes retention TTL,
//! content hashing rigor, and replay tooling.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LedgerEventKind {
    SessionStarted,
    Prompt,
    LlmCall,
    LlmResponse,
    ToolCall,
    ToolResult,
    ClaimEmitted,
    PolicyVerdict,
    BudgetDecrement,
    SessionEnded,
}

impl LedgerEventKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::SessionStarted => "session_started",
            Self::Prompt => "prompt",
            Self::LlmCall => "llm_call",
            Self::LlmResponse => "llm_response",
            Self::ToolCall => "tool_call",
            Self::ToolResult => "tool_result",
            Self::ClaimEmitted => "claim_emitted",
            Self::PolicyVerdict => "policy_verdict",
            Self::BudgetDecrement => "budget_decrement",
            Self::SessionEnded => "session_ended",
        }
    }
}

/// One event in the agent_ledger ClickHouse table. Replay returns a
/// `Vec<LedgerEvent>` in `(session_id, sequence)` order.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LedgerEvent {
    pub session_id: String,
    pub sequence: u64,
    pub timestamp_ms: u64,
    pub kind: LedgerEventKind,
    pub principal_hash: [u8; 32],
    /// Canonical JSON payload. Schema depends on `kind`.
    pub payload: String,
    pub payload_hash: [u8; 32],
    pub pre_estimate_units: u32,
    pub post_actual_units: u32,
    pub cost_relevant: bool,
    pub redaction_policy_ver: u32,
}

/// What the ClickHouse INSERT path serializes per row. `clickhouse`
/// crate prefers fixed-size types and named structs; we store the
/// hashes as hex strings for legibility (32 bytes -> 64 hex chars).
#[derive(Debug, Clone, Serialize, clickhouse::Row)]
pub struct LedgerRow {
    pub session_id: String,
    pub sequence: u64,
    pub timestamp_ms: u64,
    pub kind: String,
    pub principal_hash: String,
    pub payload: String,
    pub payload_hash: String,
    pub pre_estimate_units: u32,
    pub post_actual_units: u32,
    pub cost_relevant: u8,
    pub redaction_policy_ver: u32,
}

impl From<&LedgerEvent> for LedgerRow {
    fn from(e: &LedgerEvent) -> Self {
        Self {
            session_id: e.session_id.clone(),
            sequence: e.sequence,
            timestamp_ms: e.timestamp_ms,
            kind: e.kind.as_str().to_string(),
            principal_hash: hex_encode(&e.principal_hash),
            payload: e.payload.clone(),
            payload_hash: hex_encode(&e.payload_hash),
            pre_estimate_units: e.pre_estimate_units,
            post_actual_units: e.post_actual_units,
            cost_relevant: if e.cost_relevant { 1 } else { 0 },
            redaction_policy_ver: e.redaction_policy_ver,
        }
    }
}

#[derive(Debug, Clone, clickhouse::Row, Deserialize)]
pub struct LedgerRowRead {
    pub session_id: String,
    pub sequence: u64,
    pub timestamp_ms: u64,
    pub kind: String,
    pub principal_hash: String,
    pub payload: String,
    pub payload_hash: String,
    pub pre_estimate_units: u32,
    pub post_actual_units: u32,
    pub cost_relevant: u8,
    pub redaction_policy_ver: u32,
}

pub fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

pub fn hex_decode(s: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    let bytes = s.as_bytes();
    let len = (bytes.len() / 2).min(32);
    for i in 0..len {
        let hi = hex_nibble(bytes[i * 2]);
        let lo = hex_nibble(bytes[i * 2 + 1]);
        out[i] = (hi << 4) | lo;
    }
    out
}

fn hex_nibble(b: u8) -> u8 {
    match b {
        b'0'..=b'9' => b - b'0',
        b'a'..=b'f' => b - b'a' + 10,
        b'A'..=b'F' => b - b'A' + 10,
        _ => 0,
    }
}
