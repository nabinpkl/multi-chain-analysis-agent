use clickhouse::Row;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Row, Serialize, Deserialize)]
pub struct Edge {
    pub signature: String,
    pub instruction_idx: u16,
    pub slot: u64,
    pub block_time: u32,
    pub from_wallet: String,
    pub to_wallet: String,
    pub amount: u64,
    pub version: u64,
}
