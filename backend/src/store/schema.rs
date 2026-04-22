use clickhouse::Client;

pub async fn bootstrap(client: &Client) -> anyhow::Result<()> {
    client
        .query("CREATE DATABASE IF NOT EXISTS multichain")
        .execute()
        .await?;

    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.edges (
                signature       String,
                instruction_idx UInt16,
                slot            UInt64,
                block_time      UInt32,
                from_wallet     String,
                to_wallet       String,
                amount          UInt64,
                version         UInt64
            ) ENGINE = ReplacingMergeTree(version)
            ORDER BY (signature, instruction_idx)
            "#,
        )
        .execute()
        .await?;

    client
        .query(
            r#"
            CREATE TABLE IF NOT EXISTS multichain.ingestion_state (
                component   String,
                last_slot   UInt64,
                updated_at  DateTime
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY (component)
            "#,
        )
        .execute()
        .await?;

    client
        .query("TRUNCATE TABLE multichain.edges")
        .execute()
        .await?;

    client
        .query("TRUNCATE TABLE multichain.ingestion_state")
        .execute()
        .await?;

    Ok(())
}
