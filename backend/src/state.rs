use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::broadcast;

use crate::config::Config;
use crate::domain::Edge;
use crate::graph::GraphState;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

/// Raw-stream channel is larger: one message per ingested edge, not
/// per tick, so bursty ingestion doesn't lag slow subscribers.
const RAW_BROADCAST_CAPACITY: usize = 4096;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    /// Per-edge broadcast. Fires once per Kafka message in state-sink,
    /// consumed by `/graph/raw/stream` subscribers.
    pub raw_tx: broadcast::Sender<Arc<Edge>>,
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

        let (raw_tx, _) = broadcast::channel(RAW_BROADCAST_CAPACITY);

        Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            raw_tx,
            graph: Arc::new(RwLock::new(GraphState::default())),
        }
    }
}
