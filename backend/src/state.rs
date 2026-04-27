use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::broadcast;

use crate::config::Config;
use crate::graph::delta::GraphDelta;
use crate::graph::GraphState;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

/// Delta broadcast channel capacity. One message per ingest batch (not per
/// edge), so this covers ~4k batches of deltas before slow subscribers lag.
const DELTA_BROADCAST_CAPACITY: usize = 4096;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    /// Delta broadcast. Fires once per ingest call from the graph-engine
    /// consumer task. Consumed by `/graph/stream` SSE subscribers.
    pub delta_tx: broadcast::Sender<Arc<Vec<GraphDelta>>>,
    /// In-memory graph engine: node interner + adjacency + Union-Find.
    pub graph: Arc<RwLock<GraphState>>,
}

impl AppState {
    pub fn new(config: &Config) -> Self {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let ch_store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));

        let (delta_tx, _) = broadcast::channel(DELTA_BROADCAST_CAPACITY);

        Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            delta_tx,
            graph: Arc::new(RwLock::new(GraphState::default())),
        }
    }
}
