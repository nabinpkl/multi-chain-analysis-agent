//! Wire format and channels for backend community-detection output.
//!
//! Each rolling window has its own analytics task. The task computes a
//! Louvain partition every 3s and pushes:
//!   - a `broadcast` of `AnalyticsBatch` deltas for live SSE subscribers
//!   - a `watch` of the latest `AnalyticsSnapshot` for cold-start bootstrap
//!
//! The watch carries the full label map so a freshly connected client
//! can rebuild state from a single snapshot before tailing batches.
use std::sync::Arc;

use rustc_hash::FxHashMap;
use serde::Serialize;
use tokio::sync::{broadcast, watch};
use ts_rs::TS;

use crate::analytics::roles::NodeRole;
use crate::graph::window::NUM_WINDOWS;

/// Broadcast capacity per window for analytics batches. Sized for the
/// 3s tick cadence: with a slow subscriber, allow ~5min of buffered
/// batches before lag is reported.
const ANALYTICS_BROADCAST_CAPACITY: usize = 128;

/// Live diff between two analytics ticks. Carries community label
/// deltas (Louvain output) and role label deltas (token-mint,
/// tip-account, mev-searcher, hub variants, whale, mpc-member, normal).
/// The frontend writes them straight into its `nodeIdx -> X` ref maps.
/// Removals list nodes that left the snapshot entirely (expired from
/// the window) so the frontend can drop them from its maps.
#[derive(Serialize, TS, Clone, Debug)]
#[ts(export, export_to = "../../frontend/src/lib/generated/")]
pub struct AnalyticsBatch {
    /// Monotonic per-window sequence number. Used both as the SSE id
    /// and as a frontend de-dupe key on reconnect.
    pub epoch: u32,
    /// `(node_idx, community_id)` pairs that need writing.
    pub community_changes: Vec<(u32, u32)>,
    /// Node indices to drop from the community map.
    pub community_removals: Vec<u32>,
    /// `(node_idx, role)` pairs that need writing into the role map.
    pub role_changes: Vec<(u32, NodeRole)>,
    /// Node indices to drop from the role map.
    pub role_removals: Vec<u32>,
}

/// Internal snapshot, never crosses the wire. Kept on a `watch`
/// channel so a new SSE subscriber can read the latest labels in O(1)
/// without subscribing to broadcasts retroactively. Carries both
/// community labels and role labels so the bootstrap path can emit a
/// single AnalyticsBatch covering both maps.
#[derive(Clone, Debug, Default)]
pub struct AnalyticsSnapshot {
    pub epoch: u32,
    pub labels: FxHashMap<u32, u32>,
    pub roles: FxHashMap<u32, NodeRole>,
}

/// Read-side channels stored on `AppState`. `txs[w]` is cloned by SSE
/// handlers via `subscribe()`; `snapshots[w]` is read in bootstrap.
#[derive(Clone)]
pub struct AnalyticsChannels {
    pub txs: [broadcast::Sender<Arc<AnalyticsBatch>>; NUM_WINDOWS],
    pub snapshots: [watch::Receiver<Arc<AnalyticsSnapshot>>; NUM_WINDOWS],
}

impl AnalyticsChannels {
    /// Build the channels and return the per-window `watch::Sender`
    /// array alongside. The state struct keeps the read sides; the
    /// task spawner consumes the senders, one per window.
    pub fn new() -> (Self, [watch::Sender<Arc<AnalyticsSnapshot>>; NUM_WINDOWS]) {
        let mut tx_senders: Vec<broadcast::Sender<Arc<AnalyticsBatch>>> =
            Vec::with_capacity(NUM_WINDOWS);
        for _ in 0..NUM_WINDOWS {
            tx_senders.push(broadcast::channel(ANALYTICS_BROADCAST_CAPACITY).0);
        }
        let txs: [broadcast::Sender<Arc<AnalyticsBatch>>; NUM_WINDOWS] = tx_senders
            .try_into()
            .ok()
            .expect("NUM_WINDOWS broadcast senders");

        let mut snap_senders: Vec<watch::Sender<Arc<AnalyticsSnapshot>>> =
            Vec::with_capacity(NUM_WINDOWS);
        let mut snap_receivers: Vec<watch::Receiver<Arc<AnalyticsSnapshot>>> =
            Vec::with_capacity(NUM_WINDOWS);
        for _ in 0..NUM_WINDOWS {
            let (tx, rx) = watch::channel(Arc::new(AnalyticsSnapshot::default()));
            snap_senders.push(tx);
            snap_receivers.push(rx);
        }
        let snapshots: [watch::Receiver<Arc<AnalyticsSnapshot>>; NUM_WINDOWS] =
            snap_receivers
                .try_into()
                .ok()
                .expect("NUM_WINDOWS snapshot receivers");
        let snap_senders: [watch::Sender<Arc<AnalyticsSnapshot>>; NUM_WINDOWS] = snap_senders
            .try_into()
            .ok()
            .expect("NUM_WINDOWS snapshot senders");

        (Self { txs, snapshots }, snap_senders)
    }

    pub fn sender(&self, window_idx: usize) -> &broadcast::Sender<Arc<AnalyticsBatch>> {
        &self.txs[window_idx]
    }
}
