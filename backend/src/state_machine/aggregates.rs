use std::collections::HashSet;

use crate::state_machine::WalletId;

#[derive(Debug, Clone, Default)]
pub struct EdgeAgg {
    pub volume: u64,
    pub tx_count: u64,
}

#[derive(Debug, Clone, Default)]
pub struct WalletAgg {
    pub volume: u64,
}

#[derive(Debug, Default)]
pub struct RunningStats {
    pub total_volume: u64,
    pub total_txs: u64,
    pub unique_wallets: HashSet<WalletId>,
}
