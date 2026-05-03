//! Per-turn `WindowSnapshot` lease. Phase A of the Python-agent
//! migration introduced this so the orchestrator (Python) can hold a
//! consistent view across multiple primitive calls in one turn,
//! independent of new blocks ingesting mid-turn.
//!
//! Lifecycle:
//! 1. Python calls `POST /turn/begin`. Rust takes one read of
//!    `GraphState` + the analytics watch, materializes the 60s
//!    `TurnSnapshot`, stashes it under a fresh `snapshot_id`,
//!    returns `{snapshot_id, expires_at_ms}`.
//! 2. Every primitive call this turn passes `snapshot_id` in the
//!    body. The route looks up the `Arc<TurnSnapshot>` and reads
//!    from it. True read consistency across primitives.
//! 3. Python calls `POST /turn/end` to release. Idempotent; missing
//!    snapshot is a no-op.
//! 4. A background GC sweep drops anything not released within 5
//!    minutes. On Rust restart all snapshots vanish; Python sees
//!    410 Gone and retries `/turn/begin`.

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use rustc_hash::FxHashMap;

use crate::analytics::AnalyticsSnapshot;
use crate::analytics::snapshot::WindowSnapshot;
use crate::graph::GraphState;
use crate::graph::interner::NodeIdx;

/// How long a snapshot is allowed to live in the cache without an
/// explicit `/turn/end`. A turn that takes >5 min has bigger problems
/// than a stale snapshot.
pub const SNAPSHOT_TTL: Duration = Duration::from_secs(300);

/// How often the GC sweep runs.
pub const GC_INTERVAL: Duration = Duration::from_secs(60);

/// What the agent sees for one turn. All fields are owned (or
/// behind an Arc to a clone) so reads happen entirely off the live
/// graph lock once the snapshot is built.
pub struct TurnSnapshot {
    pub snapshot_id: String,
    pub created_at_ms: u64,
    pub expires_at_ms: u64,
    pub window_secs: u32,
    pub window_idx: usize,

    /// Per-window adjacency, components, node stats. Built once at
    /// `/turn/begin` from a brief read lock on `GraphState`.
    pub graph: WindowSnapshot,

    /// Cloned analytics snapshot for the same window: roles + labels.
    /// Cheap clone via the Arc the watch channel hands out.
    pub analytics: Arc<AnalyticsSnapshot>,

    /// Reverse interner for nodes seen in this window. Limited scope
    /// (window-local), not the full graph.
    pub idx_to_addr: FxHashMap<NodeIdx, String>,

    /// Forward interner for the same scope. Window-local; primitives
    /// that look up an address not in this window get the same
    /// `NotInWindow` error semantics as before.
    pub addr_to_idx: FxHashMap<String, NodeIdx>,
}

impl TurnSnapshot {
    /// Materialize a snapshot at the given window. Takes one brief
    /// read lock on the graph + one cheap watch borrow on analytics.
    pub fn build(
        snapshot_id: String,
        window_idx: usize,
        window_secs: u32,
        now_ms: u64,
        graph_state: &parking_lot::RwLock<GraphState>,
        analytics: Arc<AnalyticsSnapshot>,
    ) -> Arc<Self> {
        // Brief read lock: build the per-window snapshot AND the
        // window-local interner maps off the same lock so they're
        // consistent with each other.
        let (graph_snap, idx_to_addr, addr_to_idx) = {
            let g = graph_state.read();
            let snap = crate::analytics::snapshot::snapshot_window(&g, window_idx);
            // Walk every node referenced by the window snapshot
            // (as a node_stats key OR an adjacency key) and capture
            // its pubkey under the same lock. Limiting to window-
            // local nodes keeps these maps small (~1k entries for
            // the live 60s window in current load).
            let mut idx_to_addr: FxHashMap<NodeIdx, String> = FxHashMap::default();
            for &idx in snap.node_stats.keys() {
                if let Some(pk) = g.lookup_pubkey(idx) {
                    idx_to_addr.insert(idx, pk.to_string());
                }
            }
            for idx in snap.adj.keys() {
                if !idx_to_addr.contains_key(idx) {
                    if let Some(pk) = g.lookup_pubkey(*idx) {
                        idx_to_addr.insert(*idx, pk.to_string());
                    }
                }
            }
            let mut addr_to_idx: FxHashMap<String, NodeIdx> =
                FxHashMap::with_capacity_and_hasher(idx_to_addr.len(), Default::default());
            for (&idx, addr) in &idx_to_addr {
                addr_to_idx.insert(addr.clone(), idx);
            }
            (snap, idx_to_addr, addr_to_idx)
        };

        Arc::new(Self {
            snapshot_id,
            created_at_ms: now_ms,
            expires_at_ms: now_ms.saturating_add(SNAPSHOT_TTL.as_millis() as u64),
            window_secs,
            window_idx,
            graph: graph_snap,
            analytics,
            idx_to_addr,
            addr_to_idx,
        })
    }
}

/// Process-local snapshot cache. Sharded reads via DashMap so the
/// primitive routes don't serialize on the cache lock.
#[derive(Clone, Default)]
pub struct SnapshotCache {
    inner: Arc<DashMap<String, Arc<TurnSnapshot>>>,
}

impl SnapshotCache {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&self, snap: Arc<TurnSnapshot>) {
        self.inner.insert(snap.snapshot_id.clone(), snap);
    }

    pub fn get(&self, snapshot_id: &str) -> Option<Arc<TurnSnapshot>> {
        self.inner.get(snapshot_id).map(|r| r.value().clone())
    }

    /// Idempotent. Returns whether anything was removed.
    pub fn remove(&self, snapshot_id: &str) -> bool {
        self.inner.remove(snapshot_id).is_some()
    }

    pub fn len(&self) -> usize {
        self.inner.len()
    }

    /// Drop expired snapshots. Returns the count removed for telemetry.
    pub fn sweep_expired(&self, now_ms: u64) -> usize {
        let mut to_remove: Vec<String> = Vec::new();
        for entry in self.inner.iter() {
            if entry.value().expires_at_ms < now_ms {
                to_remove.push(entry.key().clone());
            }
        }
        for k in &to_remove {
            self.inner.remove(k);
        }
        to_remove.len()
    }
}

/// Spawn the GC sweep task. Runs forever; cancelled when the runtime
/// exits.
pub fn spawn_gc(cache: SnapshotCache) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(GC_INTERVAL);
        loop {
            interval.tick().await;
            let now_ms = current_time_ms();
            let removed = cache.sweep_expired(now_ms);
            if removed > 0 {
                tracing::info!(
                    removed,
                    remaining = cache.len(),
                    "snapshot_cache_gc_swept"
                );
            }
        }
    });
}

pub fn current_time_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
