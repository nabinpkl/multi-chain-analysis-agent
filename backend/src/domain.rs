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

/// `name / symbol / uri` for a mint, fetched on-demand by
/// `metadata::fetch::fetch_token_metadata` and cached in
/// `multichain.token_metadata`. Either decoded from a Metaplex Token
/// Metadata PDA account or pulled out of the Token-2022 metadata
/// extension on the mint account itself.
///
/// `name`, `symbol`, and `uri` are user-supplied at mint creation
/// time and are NOT validated by the runtime; treat them as untrusted
/// text. The agent tool surfacing these strings is intended to be
/// gated by `channels.external_text_input_enabled`; the gate itself
/// is not yet wired.
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
    /// `"metaplex"` (Metaplex Token Metadata PDA) or
    /// `"token2022"` (SPL Token-2022 metadata extension). Distinct
    /// from `op` so a query can filter by source program without
    /// enumerating per-op string values.
    pub program: String,
    /// Lazy-fetch path always emits `"fetch"`. Kept as a
    /// `LowCardinality(String)` column so the row schema can carry
    /// future op variants without a migration.
    pub op: String,
    /// `name` field. Capped at 32 bytes by Metaplex.
    pub name: String,
    /// `symbol` field. Capped at 10 bytes.
    pub symbol: String,
    /// `uri` field. Capped at 200 bytes. Points to off-chain JSON
    /// somewhere on the internet (HTTPS, IPFS, Arweave, custom
    /// gateways). Off-chain fetch is a separate ingestion leg.
    pub uri: String,
    /// Account that can sign future updates. Read from the Metaplex
    /// PDA's `update_authority` field, or empty for Token-2022 paths.
    pub update_authority: String,
    /// `ReplacingMergeTree` version, set to ingest epoch_ms.
    pub version: u64,
}

