use async_trait::async_trait;
use clickhouse::Client;
use clickhouse::Row;
use serde::Serialize;

use super::EdgeStore;
use crate::domain::{Edge, Memo};

pub struct ClickHouseEdgeStore {
    client: Client,
}

impl ClickHouseEdgeStore {
    pub fn new(client: Client) -> Self {
        Self { client }
    }
}

#[derive(Row, Serialize)]
struct CheckpointRow<'a> {
    component: &'a str,
    last_slot: u64,
    updated_at: u32,
}

#[async_trait]
impl EdgeStore for ClickHouseEdgeStore {
    async fn insert_edges(&self, edges: &[Edge]) -> anyhow::Result<()> {
        if edges.is_empty() {
            return Ok(());
        }
        let mut insert = self.client.insert("multichain.edges")?;
        for edge in edges {
            insert.write(edge).await?;
        }
        insert.end().await?;
        Ok(())
    }

    async fn insert_memos(&self, memos: &[Memo]) -> anyhow::Result<()> {
        if memos.is_empty() {
            return Ok(());
        }
        let mut insert = self.client.insert("multichain.memos")?;
        for memo in memos {
            insert.write(memo).await?;
        }
        insert.end().await?;
        Ok(())
    }

    async fn get_last_slot(&self, component: &str) -> anyhow::Result<Option<u64>> {
        let row: Option<u64> = self
            .client
            .query(
                "SELECT max(last_slot) FROM multichain.ingestion_state \
                 WHERE component = ? GROUP BY component",
            )
            .bind(component)
            .fetch_optional()
            .await?;
        Ok(row)
    }

    async fn set_last_slot(&self, component: &str, slot: u64) -> anyhow::Result<()> {
        let updated_at = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as u32)
            .unwrap_or(0);
        let mut insert = self.client.insert("multichain.ingestion_state")?;
        insert
            .write(&CheckpointRow {
                component,
                last_slot: slot,
                updated_at,
            })
            .await?;
        insert.end().await?;
        Ok(())
    }
}

