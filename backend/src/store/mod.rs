pub mod clickhouse_store;
pub mod schema;

use async_trait::async_trait;

use crate::domain::Edge;

#[async_trait]
pub trait EdgeStore: Send + Sync {
    async fn insert_edges(&self, edges: &[Edge]) -> anyhow::Result<()>;
    async fn get_last_slot(&self, component: &str) -> anyhow::Result<Option<u64>>;
    async fn set_last_slot(&self, component: &str, slot: u64) -> anyhow::Result<()>;
}
