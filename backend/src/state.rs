use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::{broadcast, watch};

use crate::analytics::{AnalyticsChannels, AnalyticsSnapshot};
use crate::config::Config;
use crate::graph::GraphState;
use crate::graph::delta::GraphDelta;
use crate::graph::window::NUM_WINDOWS;
use crate::snapshot::SnapshotCache;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

/// Delta broadcast channel capacity per window.
const DELTA_BROADCAST_CAPACITY: usize = 4096;

/// Per-window broadcast senders. One channel per rolling window
/// (60s, 300s, 900s, 1800s, 3600s) so each subscriber sees only the
/// deltas relevant to its window.
#[derive(Clone)]
pub struct WindowChannels {
    pub txs: [broadcast::Sender<Arc<Vec<GraphDelta>>>; NUM_WINDOWS],
}

impl WindowChannels {
    pub fn new() -> Self {
        let txs = std::array::from_fn(|_| broadcast::channel(DELTA_BROADCAST_CAPACITY).0);
        Self { txs }
    }

    pub fn sender(&self, window_idx: usize) -> &broadcast::Sender<Arc<Vec<GraphDelta>>> {
        &self.txs[window_idx]
    }
}

impl Default for WindowChannels {
    fn default() -> Self {
        Self::new()
    }
}

/// Data-plane application state. Phase C dropped every agent-side
/// field (loop, ledger, registry, policy, budget, stubs, threads,
/// claims/bindings/switches/tool_calls per-session buffers,
/// debug_public). The Python agent service on `:8003` owns the agent
/// plane end-to-end; the only surface left here is what the data plane
/// needs to serve graph + primitive routes.
#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    /// Per-window delta broadcast. Subscribers bind to one window's
    /// channel based on the `?window=` query param.
    pub deltas: WindowChannels,
    /// Per-window analytics broadcast + latest snapshot watch. Read-side
    /// only; the corresponding `watch::Sender` array is owned by the
    /// analytics tasks (see `analytics::spawn_all`).
    pub analytics: AnalyticsChannels,
    /// In-memory graph engine: node interner + adjacency + Union-Find.
    pub graph: Arc<RwLock<GraphState>>,
    /// Per-turn `WindowSnapshot` lease cache. Python opens a snapshot
    /// via `POST /turn/begin`, passes the returned `snapshot_id` on
    /// every primitive call this turn so reads are consistent across
    /// primitives, then releases via `POST /turn/end`. GC sweep drops
    /// anything older than 5 min.
    pub snapshot_cache: SnapshotCache,
}

impl AppState {
    /// Build the read-side state plus the per-window analytics
    /// `watch::Sender` array. The senders are consumed by
    /// `analytics::spawn_all` so each window-task owns its push side
    /// and `AppState` only carries the receiver side.
    pub fn new(
        config: &Config,
    ) -> (
        Self,
        [watch::Sender<Arc<AnalyticsSnapshot>>; NUM_WINDOWS],
    ) {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let ch_store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));
        let (analytics, analytics_senders) = AnalyticsChannels::new();

        let state = Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            deltas: WindowChannels::new(),
            analytics,
            graph: Arc::new(RwLock::new(GraphState::default())),
            snapshot_cache: SnapshotCache::new(),
        };
        (state, analytics_senders)
    }
}
