use std::sync::Arc;
use std::time::Duration;

use clickhouse::Client;

use crate::config::Config;
use crate::overview_cache::OverviewCache;
use crate::store::clickhouse_store::ClickHouseEdgeStore;
use crate::store::{EdgeStore, GraphStore};
use crate::tip::TipTracker;

#[derive(Clone)]
pub struct AppState {
    pub clickhouse: Client,
    pub store: Arc<dyn EdgeStore>,
    pub graph: Arc<dyn GraphStore>,
    pub tip: TipTracker,
    pub overview_cache: Arc<OverviewCache>,
}

impl AppState {
    pub fn new(config: &Config) -> Self {
        let clickhouse = Client::default()
            .with_url(&config.clickhouse_url)
            .with_user(&config.clickhouse_user)
            .with_password(&config.clickhouse_password)
            .with_database(&config.clickhouse_db);

        let ch_store = Arc::new(ClickHouseEdgeStore::new(clickhouse.clone()));

        Self {
            clickhouse,
            store: ch_store.clone(),
            graph: ch_store,
            tip: TipTracker::default(),
            overview_cache: Arc::new(OverviewCache::new(Duration::from_secs(10))),
        }
    }
}
