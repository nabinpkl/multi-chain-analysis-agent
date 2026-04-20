use std::sync::Arc;

use clickhouse::Client;

use crate::config::Config;
use crate::store::EdgeStore;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::tip::TipTracker;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub tip: TipTracker,
}

impl AppState {
    pub fn new(config: &Config) -> Self {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));

        Self {
            clickhouse,
            store,
            tip: TipTracker::default(),
        }
    }
}
