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

#[derive(Debug, Clone, Row, Deserialize)]
pub struct EdgeAggregate {
    pub from_wallet: String,
    pub to_wallet: String,
    pub volume_lamports: u64,
    pub tx_count: u64,
}

#[derive(Debug, Clone, Row, Deserialize)]
pub struct WalletAggregate {
    pub wallet: String,
    pub volume_lamports: u64,
}

#[derive(Debug, Clone, Row, Deserialize)]
pub struct WindowStats {
    pub total_volume_lamports: u64,
    pub total_txs: u64,
    pub unique_wallets: u64,
}
