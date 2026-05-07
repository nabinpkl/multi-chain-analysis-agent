pub mod clickhouse_store;
pub mod schema;

use async_trait::async_trait;

use crate::domain::{Edge, Memo};

/// Persistence trait for everything ingestion writes. Despite the name
/// (kept for now to avoid a wide rename), this covers edges, memos,
/// and the ingestion checkpoint state. A future cleanup ticket should
/// rename this to `Store` or split per-row-type.
#[async_trait]
pub trait EdgeStore: Send + Sync {
    async fn insert_edges(&self, edges: &[Edge]) -> anyhow::Result<()>;
    async fn insert_memos(&self, memos: &[Memo]) -> anyhow::Result<()>;
    async fn get_last_slot(&self, component: &str) -> anyhow::Result<Option<u64>>;
    async fn set_last_slot(&self, component: &str, slot: u64) -> anyhow::Result<()>;
}
