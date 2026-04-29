//! Backend community detection. One task per rolling window snapshots
//! the per-window edge view every 3s, runs Louvain per connected
//! component, assigns stable global ids, and broadcasts diffs to SSE
//! subscribers.
//!
//! Snapshot pattern (NOT shadow): the task takes a brief read lock,
//! copies just the data it needs, releases. Louvain runs off-lock.
//! No event subscription, no shadow adjacency mirror.
pub mod delta;
pub mod louvain;
pub mod mev;
pub mod mpc;
pub mod roles;
pub mod snapshot;
pub mod stable_labels;
pub mod task;

use std::sync::Arc;

use tokio::sync::watch;
use tokio::task::JoinHandle;

pub use delta::{AnalyticsBatch, AnalyticsChannels, AnalyticsSnapshot};
pub use roles::NodeRole;

use crate::graph::window::NUM_WINDOWS;
use crate::state::AppState;

/// Spawn one analytics task per window. Consumes the per-window
/// `watch::Sender` array returned by `AnalyticsChannels::new()`.
pub fn spawn_all(
    state: AppState,
    snapshot_senders: [watch::Sender<Arc<AnalyticsSnapshot>>; NUM_WINDOWS],
    shutdown: watch::Receiver<bool>,
) -> Vec<JoinHandle<()>> {
    let mut handles = Vec::with_capacity(NUM_WINDOWS);
    let senders: Vec<watch::Sender<Arc<AnalyticsSnapshot>>> = snapshot_senders.into_iter().collect();
    for (window_idx, snap_tx) in senders.into_iter().enumerate() {
        let s = state.clone();
        let sd = shutdown.clone();
        handles.push(tokio::spawn(async move {
            task::run(window_idx, s, snap_tx, sd).await;
        }));
    }
    handles
}
