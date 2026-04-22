use clickhouse::Row;
use serde::{Deserialize, Serialize};

pub const LAMPORTS_PER_SOL: f64 = 1_000_000_000.0;

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

#[derive(Debug, Clone, Serialize)]
pub struct WindowView {
    pub from: u32,
    pub to: u32,
    pub label: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatsView {
    pub total_volume_sol: f64,
    pub total_txs: u64,
    pub unique_wallets: u64,
    pub top_wallet: Option<String>,
    pub top_wallet_volume_sol: Option<f64>,
    /// Instantaneous tx rate over the last ~30s of data, not the full
    /// displayed window. Reported separately so the liveness pill breathes
    /// instead of smoothing to a ~3600s average.
    pub tx_per_sec_recent: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct NodeView {
    pub id: String,
    pub volume_sol: f64,
    pub component: Option<u32>,
    /// Distinct counterparties this wallet has across the full window
    /// (not just the rendered subset). Sent so the frontend can size
    /// and order nodes without recomputing degree from the edge list.
    pub degree: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct EdgeView {
    pub from: String,
    pub to: String,
    pub volume_sol: f64,
    pub tx_count: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct OverviewResponse {
    pub window: WindowView,
    pub stats: StatsView,
    pub nodes: Vec<NodeView>,
    pub edges: Vec<EdgeView>,
    pub generated_at: u32,
    /// True when the state machine doesn't yet hold a full `window_secs`
    /// of data — e.g. freshly booted and still warming up. Frontend should
    /// show the actual elapsed time in `window.label` and indicate partial.
    pub is_partial: bool,
}
