use clickhouse::Row;
use serde::{Deserialize, Serialize};

pub const LAMPORTS_PER_SOL: f64 = 1_000_000_000.0;

#[derive(Debug, Clone, Row, Serialize, Deserialize)]
pub struct Edge {
    pub signature: String,
    /// Sequence number for transfers within a single transaction.
    /// Multiple transfers (across mints or amounts) get distinct values
    /// so the (signature, instruction_idx) primary key is unique.
    pub instruction_idx: u16,
    pub slot: u64,
    pub block_time: u32,
    pub from_wallet: String,
    pub to_wallet: String,
    /// Raw base units. Lamports if `mint` is empty (native SOL),
    /// otherwise per-mint base units. Decimals are not tracked.
    pub amount: u64,
    /// Empty string for native SOL, otherwise the SPL mint pubkey.
    pub mint: String,
    /// One of `""` (regular transfer), `"mint"` (token issuance),
    /// `"burn"` (token destruction).
    pub kind: String,
    pub version: u64,
}

/// One SPL Memo program invocation. Decoded from `getBlock` jsonParsed
/// responses; carries the memo text and the signers required by the
/// memo program. See `docs/architecture/memos.md` for the schema
/// rationale and sizing inputs.
///
/// The `memo_text` field is the only untrusted-text-bearing surface in
/// the whole pipeline today. Future agent primitives that surface this
/// to the agent are gated by `channels.external_text_input_enabled`.
#[derive(Debug, Clone, Row, Serialize, Deserialize)]
pub struct Memo {
    /// base58 tx signature. Joins to `Edge::signature`.
    pub signature: String,
    pub slot: u64,
    pub block_time: u32,
    /// Position within the tx. Top-level instructions and inner-
    /// instructions share one ascending namespace (top-level first,
    /// then each inner-instruction group in order). Pairs with
    /// `is_inner` to identify the source.
    pub instruction_idx: u16,
    pub is_inner: bool,
    /// Memo program version: `"v1"` (Memo1U…) or `"v2"` (MemoSq…).
    /// `LowCardinality(String)` on the ClickHouse side.
    pub program: String,
    /// The memo text. UTF-8 string, may be empty.
    pub memo_text: String,
    /// Signers required by the memo program (always at least one).
    pub signers: Vec<String>,
    /// `ReplacingMergeTree` version, set to ingest epoch_ms (same as
    /// `Edge::version`). A retried slot publishes the same row again
    /// with the same version; the merge collapses the duplicate.
    pub version: u64,
}

