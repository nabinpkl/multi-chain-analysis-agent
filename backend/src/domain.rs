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

/// One Metaplex Token Metadata Program instruction that wrote
/// `name / symbol / uri` for a mint. Decoded by `ingest::metadata`
/// from the `data` field of `getBlock` instructions whose program ID
/// matches the Metaplex Token Metadata Program. See
/// `docs/architecture/token-metadata-ingestion.md` for the encoding
/// rationale and which discriminators we handle.
///
/// `name`, `symbol`, and `uri` are user-supplied at mint creation
/// time and are NOT validated by the runtime; treat them as untrusted
/// text. The future agent primitive that surfaces these strings to
/// the agent is gated by `channels.external_text_input_enabled`.
#[derive(Debug, Clone, Row, Serialize, Deserialize)]
pub struct TokenMetadataEvent {
    /// SPL/Token-2022 mint pubkey the metadata describes. Joins to
    /// `Edge::mint`. For Create instructions this comes from
    /// `account[1]` of the instruction's account list.
    pub mint: String,
    /// Metaplex metadata PDA derived from the mint. Carried so a
    /// future Update-instruction handler can join updates back to a
    /// known mint without re-deriving the PDA.
    pub metadata_pda: String,
    /// base58 tx signature. Joins to `Edge::signature`.
    pub signature: String,
    pub slot: u64,
    pub block_time: u32,
    /// Position within the tx, top-level instructions first then
    /// each inner-instruction group in order.
    pub instruction_idx: u16,
    pub is_inner: bool,
    /// Which on-chain program emitted this metadata write.
    /// `LowCardinality(String)` on the ClickHouse side. One of
    /// `"metaplex"` (Metaplex Token Metadata Program) or
    /// `"token2022"` (SPL Token-2022 metadata extension). Distinct
    /// from `op` so a query can filter by source program without
    /// enumerating per-op string values.
    pub program: String,
    /// Which instruction wrote this. `LowCardinality(String)` on
    /// the ClickHouse side. Program-scoped so values don't collide
    /// between programs: Metaplex emits `"create_v2"` / `"create_v3"`,
    /// Token-2022 emits `"t22_initialize"` / `"t22_update_field"`.
    /// More variants land as Update support is wired in.
    pub op: String,
    /// `name` field from `DataV2`. Capped at 32 bytes by Metaplex.
    /// Empty if absent (Update with `data: None`, currently unused).
    pub name: String,
    /// `symbol` field. Capped at 10 bytes.
    pub symbol: String,
    /// `uri` field. Capped at 200 bytes. Points to off-chain JSON
    /// somewhere on the internet (HTTPS, IPFS, Arweave, custom
    /// gateways). Off-chain fetch is a separate ingestion leg.
    pub uri: String,
    /// Account that can sign future updates. Reads from `account[4]`
    /// for Create instructions.
    pub update_authority: String,
    /// `ReplacingMergeTree` version, set to ingest epoch_ms.
    pub version: u64,
}

