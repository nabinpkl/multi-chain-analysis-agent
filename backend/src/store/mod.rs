pub mod clickhouse_store;
pub mod schema;

use async_trait::async_trait;

use crate::domain::{Edge, EdgeAggregate, WalletAggregate, WindowStats};

#[async_trait]
pub trait EdgeStore: Send + Sync {
    async fn insert_edges(&self, edges: &[Edge]) -> anyhow::Result<()>;
    async fn get_last_slot(&self, component: &str) -> anyhow::Result<Option<u64>>;
    async fn set_last_slot(&self, component: &str, slot: u64) -> anyhow::Result<()>;
}

#[async_trait]
pub trait GraphStore: Send + Sync {
    async fn top_edges(
        &self,
        from_ts: u32,
        to_ts: u32,
        limit: u32,
    ) -> anyhow::Result<Vec<EdgeAggregate>>;

    async fn top_wallets(
        &self,
        from_ts: u32,
        to_ts: u32,
        limit: u32,
    ) -> anyhow::Result<Vec<WalletAggregate>>;

    async fn window_stats(&self, from_ts: u32, to_ts: u32) -> anyhow::Result<WindowStats>;
}
