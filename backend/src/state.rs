use std::sync::Arc;

use clickhouse::Client;
use parking_lot::RwLock;
use tokio::sync::broadcast;

use crate::config::Config;
use crate::state_machine::StateMachine;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

const BROADCAST_CAPACITY: usize = 256;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
    pub state_machine: Arc<RwLock<StateMachine>>,
    pub window_secs: u32,
    /// Signal-only broadcast. Tick loop sends `()` whenever the state
    /// machine advanced; SSE handlers react by re-snapshotting for the
    /// window their connection is subscribed to.
    pub tick_tx: broadcast::Sender<()>,
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
            config.state_top_edges,
            config.state_whale_pad,
        )));

        let (tick_tx, _) = broadcast::channel(BROADCAST_CAPACITY);

        Self {
            clickhouse,
            store: ch_store,
            tip: TipTracker::default(),
            state_machine,
            window_secs: config.state_window_secs,
            tick_tx,
        }
    }
}
