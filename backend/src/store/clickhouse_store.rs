use async_trait::async_trait;
use clickhouse::Client;
use clickhouse::Row;
use serde::Serialize;

use super::{EdgeStore, GraphStore};
use crate::domain::{Edge, EdgeAggregate, WalletAggregate, WindowStats};

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

#[async_trait]
impl GraphStore for ClickHouseEdgeStore {
    async fn top_edges(
        &self,
        from_ts: u32,
        to_ts: u32,
        limit: u32,
    ) -> anyhow::Result<Vec<EdgeAggregate>> {
        let rows = self
            .client
            .query(
                "SELECT from_wallet, to_wallet, sum(amount) AS volume_lamports, \
                 count() AS tx_count \
                 FROM multichain.edges \
                 WHERE block_time >= ? AND block_time < ? \
                 GROUP BY from_wallet, to_wallet \
                 ORDER BY volume_lamports DESC \
                 LIMIT ?",
            )
            .bind(from_ts)
            .bind(to_ts)
            .bind(limit)
            .fetch_all::<EdgeAggregate>()
            .await?;
        Ok(rows)
    }

    async fn top_wallets(
        &self,
        from_ts: u32,
        to_ts: u32,
        limit: u32,
    ) -> anyhow::Result<Vec<WalletAggregate>> {
        let rows = self
            .client
            .query(
                "SELECT wallet, sum(amount) AS volume_lamports FROM ( \
                     SELECT from_wallet AS wallet, amount FROM multichain.edges \
                     WHERE block_time >= ? AND block_time < ? \
                     UNION ALL \
                     SELECT to_wallet AS wallet, amount FROM multichain.edges \
                     WHERE block_time >= ? AND block_time < ? \
                 ) \
                 GROUP BY wallet \
                 ORDER BY volume_lamports DESC \
                 LIMIT ?",
            )
            .bind(from_ts)
            .bind(to_ts)
            .bind(from_ts)
            .bind(to_ts)
            .bind(limit)
            .fetch_all::<WalletAggregate>()
            .await?;
        Ok(rows)
    }

    async fn window_stats(&self, from_ts: u32, to_ts: u32) -> anyhow::Result<WindowStats> {
        let row = self
            .client
            .query(
                "SELECT \
                     sum(amount) AS total_volume_lamports, \
                     count() AS total_txs, \
                     uniqExact(arrayJoin([from_wallet, to_wallet])) AS unique_wallets \
                 FROM multichain.edges \
                 WHERE block_time >= ? AND block_time < ?",
            )
            .bind(from_ts)
            .bind(to_ts)
            .fetch_optional::<WindowStats>()
            .await?
            .unwrap_or(WindowStats {
                total_volume_lamports: 0,
                total_txs: 0,
                unique_wallets: 0,
            });
        Ok(row)
    }
}
