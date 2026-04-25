use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::broadcast;

use crate::config::Config;
use crate::domain::Edge;
use crate::layout::PositionStore;
use crate::state_machine::StateMachine;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

const BROADCAST_CAPACITY: usize = 256;
/// Raw-stream channel is larger: one message per ingested edge, not
/// per tick, so bursty ingestion doesn't lag slow subscribers.
const RAW_BROADCAST_CAPACITY: usize = 4096;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    pub state_machine: Arc<RwLock<StateMachine>>,
    /// Persistent per-wallet x/y. Advanced by the tick loop's force
    /// sim; stamped onto NodeView by API handlers before serialization.
    /// Held under its own lock so sim writes don't starve state-machine
    /// snapshot readers.
    pub positions: Arc<RwLock<PositionStore>>,
    pub window_secs: u32,
    /// Signal-only broadcast. Tick loop sends `()` whenever a new
    /// snapshot is available; SSE handlers re-snapshot per-window on
    /// signal.
    pub tick_tx: broadcast::Sender<()>,
    /// Per-edge broadcast. Fires once per Kafka message in state-sink,
    /// consumed by `/graph/raw/stream` subscribers who paint every
    /// transaction as it arrives. No backend layout, no snapshot, no
    /// state  clients decide how to render.
    pub raw_tx: broadcast::Sender<Arc<Edge>>,
}

impl AppState {
    pub fn new(config: &Config) -> Self {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let ch_store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));

        let state_machine = Arc::new(RwLock::new(StateMachine::new(
            config.state_window_secs,
        )));

        let positions = Arc::new(RwLock::new(PositionStore::new()));

        let (tick_tx, _) = broadcast::channel(BROADCAST_CAPACITY);
        let (raw_tx, _) = broadcast::channel(RAW_BROADCAST_CAPACITY);

        Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            state_machine,
            positions,
            window_secs: config.state_window_secs,
            tick_tx,
            raw_tx,
        }
    }
}
