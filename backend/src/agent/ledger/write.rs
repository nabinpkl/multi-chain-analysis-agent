//! Ledger writer service. Sync per-event ClickHouse INSERTs in v0
//! (cheap; ship 4 promotes to async batched if load demands).
//!
//! Per-session sequence numbers are owned by the ledger service so
//! the loop does not have to coordinate. Each `write` call:
//! 1. Pulls and increments the per-session sequence counter.
//! 2. Computes payload hash (sha256 of canonical JSON).
//! 3. Inserts a row into `multichain.agent_ledger`.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use clickhouse::Client;
use parking_lot::Mutex;
use sha2::{Digest, Sha256};
use tracing::warn;

use super::event::{LedgerEvent, LedgerEventKind, LedgerRow};

#[derive(Clone)]
pub struct Ledger {
    client: Client,
    sequences: Arc<Mutex<HashMap<String, u64>>>,
}

impl Ledger {
    pub fn new(client: Client) -> Self {
        Self {
            client,
            sequences: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Write one event. v0 swallows write errors with a warn-log so a
    /// flaky ClickHouse cannot kill an in-flight session. Ship 4
    /// tightens this (ledger is the audit substrate; loss matters).
    pub async fn write(&self, draft: LedgerEventDraft) -> Result<u64> {
        let sequence = {
            let mut seqs = self.sequences.lock();
            let counter = seqs.entry(draft.session_id.clone()).or_insert(0);
            *counter += 1;
            *counter
        };
        let payload_hash = sha256(draft.payload.as_bytes());
        let event = LedgerEvent {
            session_id: draft.session_id,
            sequence,
            timestamp_ms: now_ms(),
            kind: draft.kind,
            principal_hash: draft.principal_hash,
            payload: draft.payload,
            payload_hash,
            pre_estimate_units: draft.pre_estimate_units,
            post_actual_units: draft.post_actual_units,
            cost_relevant: draft.cost_relevant,
            redaction_policy_ver: 0,
        };
        let row = LedgerRow::from(&event);
        let mut insert = self
            .client
            .insert("multichain.agent_ledger")
            .context("opening agent_ledger insert")?;
        if let Err(e) = insert.write(&row).await {
            warn!(error = %e, kind = ?event.kind, session_id = %event.session_id, "ledger write failed");
            return Err(e.into());
        }
        if let Err(e) = insert.end().await {
            warn!(error = %e, kind = ?event.kind, session_id = %event.session_id, "ledger insert end failed");
            return Err(e.into());
        }
        Ok(sequence)
    }

    /// Drop the per-session sequence counter on session end. Keeps
    /// the in-memory map bounded.
    pub fn drop_session(&self, session_id: &str) {
        let mut seqs = self.sequences.lock();
        seqs.remove(session_id);
    }
}

/// Loop-side input shape. Hash + sequence are added by the writer.
pub struct LedgerEventDraft {
    pub session_id: String,
    pub kind: LedgerEventKind,
    pub principal_hash: [u8; 32],
    pub payload: String,
    pub pre_estimate_units: u32,
    pub post_actual_units: u32,
    pub cost_relevant: bool,
}

fn sha256(bytes: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize().into()
}

pub(crate) fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
