use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use tokio::sync::watch;
use tracing::{debug, warn};

use crate::rpc::RpcClient;

const TIP_REFRESH_INTERVAL: Duration = Duration::from_secs(60);

/// Caches the chain tip so /ready can compute lag without burning per-request RPC budget.
#[derive(Clone, Default)]
pub struct TipTracker {
    slot: Arc<AtomicU64>,
}

impl TipTracker {
    pub fn current(&self) -> Option<u64> {
        let v = self.slot.load(Ordering::Relaxed);
        if v == 0 { None } else { Some(v) }
    }

    pub async fn run(self, rpc: RpcClient, mut shutdown_rx: watch::Receiver<bool>) {
        loop {
            match rpc.get_slot().await {
                Ok(s) => {
                    self.slot.store(s, Ordering::Relaxed);
                    debug!(tip = s, "tip refreshed");
                }
                Err(e) => warn!(error = %e, "tip refresh failed"),
            }
            tokio::select! {
                _ = shutdown_rx.changed() => return,
                _ = tokio::time::sleep(TIP_REFRESH_INTERVAL) => {}
            }
        }
    }
}
